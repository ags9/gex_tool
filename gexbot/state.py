"""State store — every poll the engine makes, made durable (Phase 1 spec §1-3).

`watch_state.json` answers "what am I holding right now" and is deliberately
left alone: small, atomic, human-readable, crash-recoverable. This module
answers the questions that file structurally cannot — what did the map look
like at 11:40, how did net gamma move across the session, which alerts
actually fired, how did the shadow book do last Tuesday.

Two rules shape everything here:

**Single writer, connection per operation.** The engine is the only writer,
and it opens and closes a connection for each write (the `manifest.py`
pattern). A held write handle locks the file and every reader — the API, a
notebook, `gexbot status` — fails. Readers open `read_only=True` and live in
the API, not here.

**A write failure must never reach the engine** (spec §2.3). Losing a poll
row is an inconvenience; an exception that escapes into the poll loop and
suppresses a breaker alert is a trading incident. Every public method
swallows, counts, and continues. `write_errors` is surfaced at
`/api/health` so the loss is visible rather than silent.

Nothing in this module reads or writes strategy parameters, and nothing here
may influence entry, exit, sizing, or discipline. It records; it decides
nothing.
"""
from __future__ import annotations

import datetime as dt
import random
import time
from pathlib import Path

import duckdb
from rich.console import Console

from .clock import ET, minute_of_day_et

console = Console()

# DuckDB allows one writing process OR many reading processes, enforced with a
# file lock. Engine and API both connect-per-operation, so the file is
# unlocked almost always — but "almost" is not a guarantee, and the API's
# WebSocket loop reads several times a second against an engine that writes
# every few minutes. Without a retry the two would eventually collide and
# surface as a lost poll row or a 500. The windows are milliseconds, so a
# short jittered backoff closes them completely.
# The patience is deliberately asymmetric. A busy dashboard holds read locks
# almost continuously, and with equal patience the *writer* is the one that
# loses — measured: 3 of 60 engine writes dropped under a hammering reader.
# That is the wrong way round. A poll row is irreplaceable and arrives every
# few minutes; a read is one of thousands and costs nothing to retry. So the
# engine waits ~10s before giving up and the API waits ~1.5s.
_WRITE_ATTEMPTS = 40
_READ_ATTEMPTS = 8
_RETRY_BASE_DELAY = 0.01
_RETRY_MAX_DELAY = 0.25


def connect(path: str, *, read_only: bool = False, attempts: int | None = None):
    """Open a connection, retrying while another process holds the lock.

    Only lock/configuration contention is retried. A corrupt file or a bad
    path fails immediately rather than after forty sleeps.
    """
    if attempts is None:
        attempts = _READ_ATTEMPTS if read_only else _WRITE_ATTEMPTS
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return duckdb.connect(path, read_only=read_only)
        except (duckdb.IOException, duckdb.ConnectionException) as e:
            last = e
            delay = min(_RETRY_BASE_DELAY * (2 ** attempt), _RETRY_MAX_DELAY)
            time.sleep(delay + random.uniform(0, _RETRY_BASE_DELAY))
    raise last                                  # type: ignore[misc]


SCHEMA = """
CREATE TABLE IF NOT EXISTS poll_snapshot (
    poll_id          BIGINT PRIMARY KEY,
    underlying       VARCHAR NOT NULL DEFAULT 'I:SPX',
    ts               TIMESTAMP NOT NULL,
    session_date     DATE NOT NULL,
    minute_of_day    INTEGER NOT NULL,
    spot             DOUBLE NOT NULL,
    net_gex          DOUBLE NOT NULL,
    oi_net           DOUBLE,
    flow_net         DOUBLE,
    flip             DOUBLE,
    put_wall         DOUBLE,
    call_wall        DOUBLE,
    max_accel        DOUBLE,
    max_magnet       DOUBLE,
    first_pos_above  DOUBLE,
    expiries         VARCHAR,
    model            VARCHAR NOT NULL,
    per_point        BOOLEAN NOT NULL,
    feed_connected   BOOLEAN,
    feed_trades      BIGINT,
    feed_contracts   BIGINT,
    poll_ms          INTEGER
);

CREATE TABLE IF NOT EXISTS poll_strike (
    poll_id          BIGINT NOT NULL,
    strike           DOUBLE NOT NULL,
    gex              DOUBLE NOT NULL,
    oi_gex           DOUBLE,
    flow_gex         DOUBLE,
    PRIMARY KEY (poll_id, strike)
);

CREATE TABLE IF NOT EXISTS alert_log (
    alert_id         BIGINT PRIMARY KEY,
    underlying       VARCHAR NOT NULL DEFAULT 'I:SPX',
    poll_id          BIGINT,
    ts               TIMESTAMP NOT NULL,
    channel          VARCHAR NOT NULL,
    kind             VARCHAR NOT NULL,
    title            VARCHAR NOT NULL,
    body             VARCHAR NOT NULL,
    spot             DOUBLE,
    strike           DOUBLE
);

CREATE TABLE IF NOT EXISTS shadow_trade (
    trade_id         BIGINT PRIMARY KEY,
    underlying       VARCHAR NOT NULL DEFAULT 'I:SPX',
    session_date     DATE NOT NULL,
    direction        INTEGER NOT NULL,
    strike           DOUBLE NOT NULL,
    expiry           DATE NOT NULL,
    contracts        INTEGER NOT NULL,
    trigger          VARCHAR NOT NULL,
    level            DOUBLE,
    entry_ts         TIMESTAMP NOT NULL,
    entry_minute     INTEGER NOT NULL,
    entry_spot       DOUBLE NOT NULL,
    entry_premium    DOUBLE NOT NULL,
    exit_ts          TIMESTAMP,
    exit_minute      INTEGER,
    exit_spot        DOUBLE,
    exit_premium     DOUBLE,
    exit_reason      VARCHAR,
    pnl              DOUBLE
);
"""

DEFAULT_UNDERLYING = "I:SPX"

# Applied after SCHEMA on every open. DuckDB's ADD COLUMN IF NOT EXISTS is
# idempotent and backfills existing rows with the default, so a store written
# before this column existed migrates in place on the next engine start —
# no dump-and-reload, and no window where the engine cannot write.
#
# The default is the honest value rather than a placeholder: every poll ever
# recorded came from `watch --underlying I:SPX`, which is the CLI default, so
# backfilling to I:SPX states what actually happened.
MIGRATIONS = (
    f"ALTER TABLE poll_snapshot ADD COLUMN IF NOT EXISTS underlying "
    f"VARCHAR DEFAULT '{DEFAULT_UNDERLYING}'",
    f"ALTER TABLE alert_log ADD COLUMN IF NOT EXISTS underlying "
    f"VARCHAR DEFAULT '{DEFAULT_UNDERLYING}'",
    f"ALTER TABLE shadow_trade ADD COLUMN IF NOT EXISTS underlying "
    f"VARCHAR DEFAULT '{DEFAULT_UNDERLYING}'",
    f"UPDATE poll_snapshot SET underlying = '{DEFAULT_UNDERLYING}' "
    f"WHERE underlying IS NULL",
    f"UPDATE alert_log SET underlying = '{DEFAULT_UNDERLYING}' "
    f"WHERE underlying IS NULL",
    f"UPDATE shadow_trade SET underlying = '{DEFAULT_UNDERLYING}' "
    f"WHERE underlying IS NULL",
)

# poll_strike deliberately has NO underlying column: it reaches one through
# poll_id. Denormalising it would repeat the same string across ~155 rows per
# poll and create a second place for the two to disagree.

# Alert kinds the engine may record (spec §2). Kept as a tuple so a typo at a
# call site is caught here rather than becoming an unqueryable one-off value.
ALERT_KINDS = ("regime_flip", "flip_cross", "proximity", "map",
               "shadow_entry", "shadow_exit", "digest", "warning")


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def session_date_of(ts: dt.datetime) -> dt.date:
    """The ET calendar date a timestamp belongs to.

    Derived through `clock`, never from the UTC date: after 20:00 ET the UTC
    date has already rolled over, so a UTC-derived session_date would file
    the last forty minutes of a session under tomorrow.
    """
    return ts.astimezone(ET).date()


def minute_of(ts: dt.datetime) -> int:
    return minute_of_day_et(int(ts.timestamp() * 1e9))


class StateStore:
    """Durable record of what the engine saw and did.

    Every method is best-effort: on failure it logs, counts, and returns None
    rather than raising into the poll loop.
    """

    def __init__(self, db_path: Path | str):
        self.path = str(db_path)
        self.write_errors = 0
        self.last_error: str = ""
        self.available = False
        self._last_ids: dict[str, int] = {}
        try:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            with connect(self.path) as con:
                con.execute(SCHEMA)
                for stmt in MIGRATIONS:
                    con.execute(stmt)
            self.available = True
        except Exception as e:                       # degraded, not dead
            self._note(e, "schema")

    # ── internals ────────────────────────────────────────────────────
    def _note(self, exc: Exception, what: str) -> None:
        self.write_errors += 1
        self.last_error = f"{what}: {exc}"
        console.log(f"[yellow]state write failed ({what}): {exc}")

    def _next_id(self, table: str, ts: dt.datetime) -> int:
        """`int(ts.timestamp() * 1000)`, bumped past the last id issued for
        this table so two events in the same millisecond cannot collide on
        the primary key (spec §2.1)."""
        i = int(ts.timestamp() * 1000)
        last = self._last_ids.get(table, 0)
        if i <= last:
            i = last + 1
        self._last_ids[table] = i
        return i

    # ── writers ──────────────────────────────────────────────────────
    def write_poll(self, profile: dict, feed_stats: dict | None = None,
                   poll_ms: int | None = None, *, model: str = "naive",
                   per_point: bool = True, underlying: str = DEFAULT_UNDERLYING,
                   ts: dt.datetime | None = None) -> int | None:
        """One `poll_snapshot` row plus its `poll_strike` profile.

        `profile` is whatever `levels.build_profile` (or `livefeed.combine`)
        returned. When the flow overlay is off, `oi_net`/`flow_net` and the
        per-strike split are NULL rather than zero — absent and zero are
        different facts, and only NULL says "not measured".

        The per-strike split is read from optional `oi_by_strike` /
        `flow_by_strike` keys, so wiring the live feed in later needs no
        change here.
        """
        if not self.available or not profile:
            return None
        ts = ts or _utcnow()
        try:
            pid = self._next_id("poll", ts)
            by_strike = profile.get("by_strike") or {}
            oi_bs = profile.get("oi_by_strike") or {}
            flow_bs = profile.get("flow_by_strike") or {}
            exps = profile.get("expiries") or []
            fs = feed_stats or {}
            rows = [
                (pid, float(k), float(v),
                 float(oi_bs[k]) if k in oi_bs else None,
                 float(flow_bs[k]) if k in flow_bs else None)
                for k, v in by_strike.items()
            ]
            with connect(self.path) as con:
                # named columns, not positional: a future migration appends
                # to the table and would silently shift a positional INSERT
                con.execute(
                    """INSERT INTO poll_snapshot
                       (poll_id, underlying, ts, session_date, minute_of_day,
                        spot, net_gex, oi_net, flow_net, flip, put_wall,
                        call_wall, max_accel, max_magnet, first_pos_above,
                        expiries, model, per_point, feed_connected,
                        feed_trades, feed_contracts, poll_ms)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    [pid, underlying, ts.replace(tzinfo=None), session_date_of(ts),
                     minute_of(ts), float(profile["spot"]),
                     float(profile["net"]),
                     profile.get("oi_net"), profile.get("flow_net"),
                     profile.get("flip"), profile.get("put_wall"),
                     profile.get("call_wall"), profile.get("max_accel"),
                     profile.get("max_magnet"),
                     profile.get("first_positive_above"),
                     ",".join(str(e) for e in exps), model, per_point,
                     fs.get("connected"), fs.get("trades"),
                     fs.get("contracts"), poll_ms],
                )
                if rows:
                    con.executemany(
                        "INSERT INTO poll_strike VALUES (?,?,?,?,?)", rows)
            return pid
        except Exception as e:
            self._note(e, "write_poll")
            return None

    def write_alert(self, *, channel: str, kind: str, title: str, body: str,
                    poll_id: int | None = None, spot: float | None = None,
                    strike: float | None = None,
                    underlying: str = DEFAULT_UNDERLYING,
                    ts: dt.datetime | None = None) -> int | None:
        """Record an alert that was sent. Called from the same site as the
        Discord send so the channel and the store cannot disagree."""
        if not self.available:
            return None
        ts = ts or _utcnow()
        try:
            if kind not in ALERT_KINDS:
                # record it anyway — losing the row is worse than an odd kind
                console.log(f"[yellow]unknown alert kind {kind!r}")
            aid = self._next_id("alert", ts)
            with connect(self.path) as con:
                con.execute(
                    """INSERT INTO alert_log
                       (alert_id, underlying, poll_id, ts, channel, kind,
                        title, body, spot, strike)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    [aid, underlying, poll_id, ts.replace(tzinfo=None), channel,
                     kind, title, body, spot, strike])
            return aid
        except Exception as e:
            self._note(e, "write_alert")
            return None

    def open_shadow_trade(self, *, direction: int, strike: float,
                          expiry: dt.date, contracts: int, trigger: str,
                          level: float | None, entry_spot: float,
                          entry_premium: float,
                          underlying: str = DEFAULT_UNDERLYING,
                          ts: dt.datetime | None = None) -> int | None:
        if not self.available:
            return None
        ts = ts or _utcnow()
        try:
            tid = self._next_id("trade", ts)
            with connect(self.path) as con:
                con.execute(
                    """INSERT INTO shadow_trade
                       (trade_id, underlying, session_date, direction, strike,
                        expiry, contracts, trigger, level, entry_ts,
                        entry_minute, entry_spot, entry_premium)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    [tid, underlying, session_date_of(ts), direction, float(strike),
                     expiry, int(contracts), trigger, level,
                     ts.replace(tzinfo=None), minute_of(ts),
                     float(entry_spot), float(entry_premium)])
            return tid
        except Exception as e:
            self._note(e, "open_shadow_trade")
            return None

    def close_shadow_trade(self, trade_id: int | None, *, exit_spot: float,
                           exit_premium: float, exit_reason: str, pnl: float,
                           ts: dt.datetime | None = None) -> bool:
        """Fill in the exit columns of an open row. A trade opened before the
        store existed (or whose open write failed) has no id — that is logged
        and skipped, never guessed at."""
        if not self.available:
            return False
        if trade_id is None:
            console.log("[yellow]shadow exit with no trade_id — not recorded")
            return False
        ts = ts or _utcnow()
        try:
            with connect(self.path) as con:
                con.execute(
                    """UPDATE shadow_trade SET exit_ts=?, exit_minute=?,
                       exit_spot=?, exit_premium=?, exit_reason=?, pnl=?
                       WHERE trade_id=?""",
                    [ts.replace(tzinfo=None), minute_of(ts), float(exit_spot),
                     float(exit_premium), exit_reason, float(pnl), trade_id])
            return True
        except Exception as e:
            self._note(e, "close_shadow_trade")
            return False


def default_path() -> Path:
    from .config import settings
    return Path(settings.gex_data_root) / "state.duckdb"
