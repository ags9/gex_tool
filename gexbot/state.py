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
    poll_ms          INTEGER,
    spy_ratio        DOUBLE,          -- measured SPX/SPY basis on COMPLEX polls
    net_dex          DOUBLE           -- net dealer delta. EXPOSURE, not a forecast.
);

CREATE TABLE IF NOT EXISTS poll_strike (
    poll_id          BIGINT NOT NULL,
    strike           DOUBLE NOT NULL,
    gex              DOUBLE NOT NULL,
    oi_gex           DOUBLE,
    flow_gex         DOUBLE,
    volume           DOUBLE,          -- unsigned day volume; activity, not positioning
    dex              DOUBLE,          -- dealer delta exposure. EXPOSURE only.
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

CREATE TABLE IF NOT EXISTS poll_expiry (
    poll_id       BIGINT NOT NULL,
    expiry        DATE NOT NULL,
    gamma         DOUBLE NOT NULL,   -- dealer gamma expiring ON this date
    delta         DOUBLE NOT NULL,   -- dealer delta expiring ON this date
    oi            BIGINT NOT NULL,
    put_call_oi   DOUBLE,
    PRIMARY KEY (poll_id, expiry)
);

-- The live tape window (spec §15.2).
--
-- DEVIATION, flagged: §15.2 says prints are "not persisted to DuckDB by
-- default", and the reason it gives is volume — millions per session, dwarfing
-- every other table. This table cannot have that property: it is hard-capped
-- at TAPE_CAP rows per instrument and trimmed on every write, so it holds
-- ~0.5 MB and never grows.
--
-- The alternative that avoids DuckDB entirely does worse on exactly the axis
-- the spec cares about. The engine and the API are separate processes, so the
-- in-memory ring is invisible to `GET /api/tape`; publishing it as a file
-- rewritten at the 1 s cadence the panel needs writes gigabytes a day to the
-- drive. The unbounded path the spec is actually guarding against is
-- --record-tape, which writes Parquet and stays off by default.
CREATE TABLE IF NOT EXISTS tape_print (
    seq                BIGINT PRIMARY KEY,
    underlying         VARCHAR NOT NULL,
    ts                 TIMESTAMP NOT NULL,
    session_date       DATE NOT NULL,
    minute_of_day      INTEGER NOT NULL,
    ticker             VARCHAR NOT NULL,
    root               VARCHAR NOT NULL,
    expiry             VARCHAR NOT NULL,
    strike             DOUBLE NOT NULL,
    option_right       VARCHAR NOT NULL,   -- see RESERVED_COLUMN_NAMES below
    price              DOUBLE NOT NULL,
    size               BIGINT NOT NULL,
    side               INTEGER NOT NULL,     -- +1 buy, -1 sell, 0 unclassified
    premium            DOUBLE NOT NULL,
    gamma_used         DOUBLE NOT NULL,      -- diagnostic: 0 = lookup missed
    dealer_gamma_delta DOUBLE NOT NULL       -- diagnostic: what the ledger wrote
);

-- Session counters for the tape header. Kept out of tape_print so the honesty
-- numbers survive the ring trimming away the prints they counted.
CREATE TABLE IF NOT EXISTS tape_stats (
    underlying     VARCHAR NOT NULL,
    session_date   DATE NOT NULL,
    prints_seen    BIGINT NOT NULL,
    contracts_seen BIGINT NOT NULL,
    unclassified   BIGINT NOT NULL,
    gamma_misses   BIGINT NOT NULL,
    updated_at     TIMESTAMP NOT NULL,
    PRIMARY KEY (underlying, session_date)
);

-- Paper positions the operator registered for the bot to watch (exit-manager
-- spec §3). PAPER ONLY: nothing here has ever been sent to a broker.
CREATE TABLE IF NOT EXISTS paper_position (
    position_id    BIGINT PRIMARY KEY,
    session_date   DATE NOT NULL,
    symbol         VARCHAR NOT NULL,       -- SPX | XSP
    direction      INTEGER NOT NULL,       -- +1 long call, -1 long put
    contracts      INTEGER NOT NULL,
    strike         DOUBLE NOT NULL,
    expiry         DATE,
    entry_ts       TIMESTAMP NOT NULL,
    entry_minute   INTEGER NOT NULL,
    entry_spot     DOUBLE NOT NULL,
    entry_premium  DOUBLE NOT NULL,
    target_level   DOUBLE NOT NULL,
    next_level     DOUBLE,
    status         VARCHAR NOT NULL,       -- open | closed
    closed_ts      TIMESTAMP,
    note           VARCHAR
);

-- One row per simulated tranche close, per policy. Three policies per
-- position (§3): what the operator did, close-all-at-target, and the ladder.
-- Recording only one would be unrecoverable later.
CREATE TABLE IF NOT EXISTS exit_shadow (
    shadow_id      BIGINT PRIMARY KEY,
    position_id    BIGINT NOT NULL,
    policy         VARCHAR NOT NULL,       -- manual | target_all | ladder
    rule           VARCHAR NOT NULL,
    ts             TIMESTAMP NOT NULL,
    minute_of_day  INTEGER NOT NULL,
    spot           DOUBLE NOT NULL,
    mark           DOUBLE NOT NULL,
    fill_price     DOUBLE NOT NULL,
    contracts      INTEGER NOT NULL,
    spread_cost    DOUBLE NOT NULL,        -- §2.6, per tranche
    commission     DOUBLE NOT NULL,
    pnl            DOUBLE NOT NULL
);

CREATE TABLE IF NOT EXISTS narration (
    poll_id       BIGINT PRIMARY KEY,
    ts            TIMESTAMP NOT NULL,
    model         VARCHAR NOT NULL,
    text          VARCHAR NOT NULL,
    -- Only linted-clean text is stored. A violation is logged and dropped,
    -- so this table never holds a sentence the lint would reject.
    lint_ok       BOOLEAN NOT NULL
);

CREATE TABLE IF NOT EXISTS poll_premium (
    poll_id          BIGINT PRIMARY KEY,
    ts               TIMESTAMP NOT NULL,
    session_date     DATE NOT NULL,
    minute_of_day    INTEGER NOT NULL,
    call_bought      DOUBLE NOT NULL,
    call_sold        DOUBLE NOT NULL,
    put_bought       DOUBLE NOT NULL,
    put_sold         DOUBLE NOT NULL,
    trades_counted   BIGINT NOT NULL,
    unclassified     BIGINT NOT NULL
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
    pnl              DOUBLE,
    -- Greeks as they stood at entry. RECORDED, NEVER USED FOR SELECTION:
    -- nothing reads these back into a trading decision, and CLAUDE.md §3
    -- freezes the code that would. They exist so a later review can ask what
    -- the book was actually exposed to, which the strike alone cannot say.
    entry_delta      DOUBLE,
    entry_gamma      DOUBLE,
    entry_theta      DOUBLE,
    entry_vega       DOUBLE,
    entry_iv         DOUBLE
);
"""

DEFAULT_UNDERLYING = "I:SPX"

# ⚙ Ring size per instrument, matching the in-memory buffer on the feed.
TAPE_CAP = 2000

# Words DuckDB rejects as BARE column identifiers — measured against the
# engine, not recalled. `right` is the one that bit us: it failed schema
# creation outright, which cascaded into 30 test failures.
#
# The fix is always to rename the column, never to quote it. A quoted
# identifier works but only for as long as every future query remembers the
# quotes, and the one that forgets fails at runtime rather than at schema
# creation. A name that needs no quoting cannot be got wrong.
#
# Note `range` is NOT reserved in DuckDB, despite looking like it should be.
RESERVED_COLUMN_NAMES = frozenset({
    "all", "and", "any", "as", "asc", "case", "cast", "check", "collate",
    "column", "constraint", "create", "default", "desc", "describe",
    "distinct", "else", "end", "except", "from", "full", "glob", "group",
    "having", "in", "inner", "intersect", "into", "is", "join", "left",
    "like", "limit", "natural", "not", "null", "offset", "on", "or", "order",
    "outer", "pivot", "primary", "qualify", "references", "returning",
    "right", "select", "similar", "some", "summarize", "symmetric", "table",
    "then", "union", "unique", "unpivot", "using", "when", "where", "window",
})

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
    "ALTER TABLE poll_strike ADD COLUMN IF NOT EXISTS volume DOUBLE",
    "ALTER TABLE poll_snapshot ADD COLUMN IF NOT EXISTS spy_ratio DOUBLE",
    "ALTER TABLE poll_snapshot ADD COLUMN IF NOT EXISTS net_dex DOUBLE",
    "ALTER TABLE poll_strike ADD COLUMN IF NOT EXISTS dex DOUBLE",
    *(f"ALTER TABLE shadow_trade ADD COLUMN IF NOT EXISTS {c} DOUBLE"
      for c in ("entry_delta", "entry_gamma", "entry_theta", "entry_vega",
                "entry_iv")),
)

# poll_strike deliberately has NO underlying column: it reaches one through
# poll_id. Denormalising it would repeat the same string across ~155 rows per
# poll and create a second place for the two to disagree.

# Alert kinds the engine may record (spec §2). Kept as a tuple so a typo at a
# call site is caught here rather than becoming an unqueryable one-off value.
ALERT_KINDS = ("regime_flip", "flip_cross", "proximity", "map",
               "shadow_entry", "shadow_exit", "digest", "warning")


# (old name, new name) pairs applied before SCHEMA, so a store written by an
# earlier build is migrated in place rather than left with a stale column the
# queries no longer reference.
_COLUMN_RENAMES = (("tape_print", "opt_right", "option_right"),)


def _rename_legacy_columns(con) -> None:
    """Idempotent: checks information_schema first, since ALTER ... RENAME
    COLUMN has no IF EXISTS and would fail on a store already migrated."""
    for table, old, new in _COLUMN_RENAMES:
        try:
            present = con.execute(
                """SELECT count(*) FROM information_schema.columns
                   WHERE table_name = ? AND column_name = ?""",
                [table, old]).fetchone()[0]
            if present:
                con.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")
                console.log(f"[dim]migrated {table}.{old} -> {new}")
        except Exception as e:                  # never block the engine
            console.log(f"[yellow]column rename {table}.{old} failed: {e}")


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
                _rename_legacy_columns(con)
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
            vol_bs = profile.get("volume_by_strike") or {}
            dex_bs = profile.get("dex_by_strike") or {}
            exps = profile.get("expiries") or []
            fs = feed_stats or {}
            rows = [
                (pid, float(k), float(v),
                 float(oi_bs[k]) if k in oi_bs else None,
                 float(flow_bs[k]) if k in flow_bs else None,
                 float(vol_bs[k]) if k in vol_bs else None,
                 float(dex_bs[k]) if k in dex_bs else None)
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
                        feed_trades, feed_contracts, poll_ms, spy_ratio,
                        net_dex)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                     fs.get("contracts"), poll_ms, profile.get("spy_ratio"),
                     profile.get("dex")],
                )
                exp_rows = [
                    (pid, e["expiry"], float(e["gamma"]), float(e["delta"]),
                     int(e["oi"]), e.get("put_call_oi"))
                    for e in (profile.get("expiry_profile") or [])
                ]
                if rows:
                    con.executemany(
                        """INSERT INTO poll_strike
                           (poll_id, strike, gex, oi_gex, flow_gex, volume, dex)
                           VALUES (?,?,?,?,?,?,?)""", rows)
                if exp_rows:
                    con.executemany(
                        """INSERT OR REPLACE INTO poll_expiry
                           (poll_id, expiry, gamma, delta, oi, put_call_oi)
                           VALUES (?,?,?,?,?,?)""", exp_rows)
            return pid
        except Exception as e:
            self._note(e, "write_poll")
            return None

    def write_premium(self, poll_id: int, premium: dict,
                      ts: dt.datetime | None = None) -> bool:
        """Cumulative session-to-date premium by side (spec §5).

        Four totals, not two: "paid $9.6M for puts" and "sold $9.6M of puts"
        are opposite facts and an unsigned total conflates them. `unclassified`
        rides along because zero-tick prints that inherit no direction are
        excluded from all four — if that count is large the panel means less,
        and the operator has to be able to see that.
        """
        if not self.available or poll_id is None:
            return False
        ts = ts or _utcnow()
        try:
            with connect(self.path) as con:
                con.execute(
                    """INSERT OR REPLACE INTO poll_premium
                       (poll_id, ts, session_date, minute_of_day, call_bought,
                        call_sold, put_bought, put_sold, trades_counted,
                        unclassified)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    [poll_id, ts.replace(tzinfo=None), session_date_of(ts),
                     minute_of(ts),
                     float(premium.get("call_bought", 0.0)),
                     float(premium.get("call_sold", 0.0)),
                     float(premium.get("put_bought", 0.0)),
                     float(premium.get("put_sold", 0.0)),
                     int(premium.get("trades_counted", 0)),
                     int(premium.get("unclassified", 0))])
            return True
        except Exception as e:
            self._note(e, "write_premium")
            return False

    def write_tape(self, prints: list, underlying: str,
                   stats: dict | None = None, *, cap: int = TAPE_CAP) -> int:
        """Publish a batch of prints and trim the ring to `cap`.

        Trimming happens in the same transaction as the insert, so the table
        is never briefly unbounded — the cap is a property of the table, not
        of how often something remembers to prune it.
        """
        if not self.available or not prints:
            return 0
        try:
            rows = []
            for p in prints:
                ts = dt.datetime.fromtimestamp(p.ts, tz=dt.timezone.utc)
                rows.append((self._next_id("tape", ts), underlying,
                             ts.replace(tzinfo=None), session_date_of(ts),
                             minute_of(ts), p.ticker, p.root, p.expiry,
                             float(p.strike), p.right, float(p.price),
                             int(p.size), int(p.side), float(p.premium),
                             float(p.gamma_used), float(p.dealer_gamma_delta)))
            with connect(self.path) as con:
                con.executemany(
                    """INSERT OR REPLACE INTO tape_print VALUES
                       (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
                # Trim by RANK, not by seq arithmetic: seq is a millisecond
                # timestamp, not a dense counter, so `max(seq) - cap` subtracts
                # 2000 milliseconds rather than 2000 rows and leaves whatever
                # number of prints happened to arrive in that window.
                con.execute(
                    """DELETE FROM tape_print WHERE underlying = ?
                       AND seq NOT IN (SELECT seq FROM tape_print
                                       WHERE underlying = ?
                                       ORDER BY seq DESC LIMIT ?)""",
                    [underlying, underlying, cap])
                if stats:
                    con.execute(
                        """INSERT OR REPLACE INTO tape_stats VALUES
                           (?,?,?,?,?,?,?)""",
                        [underlying, session_date_of(dt.datetime.now(dt.timezone.utc)),
                         int(stats.get("prints_seen", 0)),
                         int(stats.get("contracts_seen", 0)),
                         int(stats.get("unclassified", 0)),
                         int(stats.get("gamma_misses", 0)),
                         _utcnow().replace(tzinfo=None)])
            return len(rows)
        except Exception as e:
            self._note(e, "write_tape")
            return 0

    def open_paper_position(self, *, symbol: str, direction: int,
                            contracts: int, strike: float, expiry: dt.date | None,
                            entry_spot: float, entry_premium: float,
                            target_level: float, next_level: float | None = None,
                            note: str | None = None,
                            ts: dt.datetime | None = None) -> int | None:
        """Register a position for the bot to watch. PAPER ONLY — this places
        nothing and reaches no broker (spec §0.1)."""
        if not self.available:
            return None
        ts = ts or _utcnow()
        try:
            pid = self._next_id("paper", ts)
            with connect(self.path) as con:
                con.execute(
                    """INSERT INTO paper_position
                       (position_id, session_date, symbol, direction, contracts,
                        strike, expiry, entry_ts, entry_minute, entry_spot,
                        entry_premium, target_level, next_level, status,
                        closed_ts, note)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'open',NULL,?)""",
                    [pid, session_date_of(ts), symbol, int(direction),
                     int(contracts), float(strike), expiry,
                     ts.replace(tzinfo=None), minute_of(ts), float(entry_spot),
                     float(entry_premium), float(target_level), next_level, note])
            return pid
        except Exception as e:
            self._note(e, "open_paper_position")
            return None

    def close_paper_position(self, position_id: int,
                             ts: dt.datetime | None = None) -> bool:
        if not self.available:
            return False
        ts = ts or _utcnow()
        try:
            with connect(self.path) as con:
                con.execute(
                    "UPDATE paper_position SET status='closed', closed_ts=? "
                    "WHERE position_id=?", [ts.replace(tzinfo=None), position_id])
            return True
        except Exception as e:
            self._note(e, "close_paper_position")
            return False

    def write_shadow_fills(self, position_id: int, fills: list,
                           ts: dt.datetime | None = None) -> int:
        """Persist simulated tranche closes. `fills` are shadow.Fill objects."""
        if not self.available or not fills:
            return 0
        base = ts or _utcnow()
        try:
            rows = []
            for f in fills:
                rows.append((self._next_id("shadow", base), position_id,
                             f.policy.value, f.rule.value,
                             base.replace(tzinfo=None), int(f.minute),
                             float(f.spot), float(f.mark), float(f.fill_price),
                             int(f.contracts), float(f.spread_cost),
                             float(f.commission), float(f.pnl)))
            with connect(self.path) as con:
                con.executemany(
                    "INSERT INTO exit_shadow VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    rows)
            return len(rows)
        except Exception as e:
            self._note(e, "write_shadow_fills")
            return 0

    def write_narration(self, poll_id: int, text: str, model: str,
                        ts: dt.datetime | None = None) -> bool:
        """Store a narration against the poll it describes (spec §13.3).

        The poll_id and model version travel with the text so the prose can
        be audited against the numbers that produced it — which is the only
        way to catch a prompt that has drifted into implying things.
        """
        if not self.available or poll_id is None:
            return False
        try:
            with connect(self.path) as con:
                con.execute(
                    """INSERT OR REPLACE INTO narration
                       (poll_id, ts, model, text, lint_ok) VALUES (?,?,?,?,?)""",
                    [poll_id, (ts or _utcnow()).replace(tzinfo=None), model,
                     text, True])
            return True
        except Exception as e:
            self._note(e, "write_narration")
            return False

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
                          greeks: dict | None = None,
                          ts: dt.datetime | None = None) -> int | None:
        """`greeks` is recorded and never read back into a decision — see the
        column comment in SCHEMA. Passing None records NULLs rather than
        zeros: an unavailable greek is not a delta of zero."""
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
                        entry_minute, entry_spot, entry_premium,
                        entry_delta, entry_gamma, entry_theta, entry_vega,
                        entry_iv)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    [tid, underlying, session_date_of(ts), direction, float(strike),
                     expiry, int(contracts), trigger, level,
                     ts.replace(tzinfo=None), minute_of(ts),
                     float(entry_spot), float(entry_premium),
                     *( (g.get("delta"), g.get("gamma"), g.get("theta"),
                         g.get("vega"), g.get("iv"))
                        if (g := greeks or None) else (None,) * 5 )])
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
