"""Read-only access to the state store and the results bundles on disk.

Separate from `state.py` on purpose (spec §3.1): the engine owns writing, the
API owns reading, and neither imports the other's half. Every connection here
is opened `read_only=True` and closed immediately — a reader that holds a
handle is indistinguishable from a writer to DuckDB, and would lock the file
against the engine it is supposed to be observing.

Nothing in this module can mutate anything. That is not a convention, it is
the file's entire job.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from ..clock import ET
from ..config import settings
from ..state import DEFAULT_UNDERLYING, connect, default_path

ALL_UNDERLYINGS = "*"        # explicit opt-out of the I:SPX default

# Market hours in ET minutes-of-day, used only to decide whether silence from
# the engine is alarming or expected.
SESSION_OPEN = 9 * 60 + 30
SESSION_CLOSE = 16 * 60
STALE_SECONDS = 300          # spec §4.3: >5 min without a poll in-hours = not ok


def _underlying_clause(underlying: str | None) -> tuple[str, list]:
    """Filter, defaulting to I:SPX (spec §10.2).

    The default matters now that SPY and COMPLEX write their own rows: an
    unfiltered "latest poll" would return whichever instrument happened to be
    written last, so the screen would silently change instrument between
    refreshes. ALL_UNDERLYINGS is the explicit opt-out.
    """
    if underlying == ALL_UNDERLYINGS:
        return ("", [])
    return (" WHERE underlying = ?", [underlying or DEFAULT_UNDERLYING])


def _connect(path: Path | str):
    """Read-only, and retrying: the engine holds the write lock for a few
    milliseconds per poll, and a read that lands inside that window must
    wait rather than 500."""
    return connect(str(path), read_only=True)


def _dicts(con, sql: str, params: list | None = None) -> list[dict]:
    cur = con.execute(sql, params or [])
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _dicts_optional(con, sql: str, params: list | None = None) -> list[dict]:
    """For tables a store may predate. A reader that 500s because the engine
    has not yet created a table is reporting its own age as a server fault;
    an empty list is the truthful answer — there are no rows."""
    try:
        return _dicts(con, sql, params)
    except duckdb.CatalogException:
        return []


@dataclass
class _FooterState:
    """The slice of WatchState that position_footer actually reads."""
    open_trade: dict | None = None
    closed_trades: list = field(default_factory=list)
    shadow_pnl: float = 0.0


class StateReader:
    def __init__(self, db_path: Path | str | None = None):
        self.path = Path(db_path or default_path())

    @property
    def exists(self) -> bool:
        return self.path.exists()

    # ── health ───────────────────────────────────────────────────────
    def health(self, *, now: dt.datetime | None = None) -> dict:
        """`ok` answers one question: can the UI trust what it is showing?

        A store that does not exist yet, or an engine that has gone quiet
        during market hours, both mean no. Outside market hours silence is
        the correct state, so it is not held against the engine.
        """
        now = now or dt.datetime.now(dt.timezone.utc)
        et = now.astimezone(ET)
        in_hours = (et.weekday() < 5
                    and SESSION_OPEN <= et.hour * 60 + et.minute <= SESSION_CLOSE)
        out = {
            "ok": False,
            "engine_last_poll_ts": None,
            "engine_stale_seconds": None,
            "feed_connected": None,
            "feed_trades": None,
            "feed_contracts": None,
            "write_errors": None,
            "market_hours": in_hours,
            "db": str(self.path),
            "version": _version(),
            "as_of": None,
        }
        if not self.exists:
            out["detail"] = "no state store yet — engine has never run"
            return out
        with _connect(self.path) as con:
            rows = _dicts(con, """SELECT ts, feed_connected, feed_trades,
                                         feed_contracts
                                  FROM poll_snapshot
                                  ORDER BY poll_id DESC LIMIT 1""")
        if not rows:
            out["detail"] = "state store is empty — engine has not polled"
            return out
        last = rows[0]
        ts = last["ts"].replace(tzinfo=dt.timezone.utc)
        stale = (now - ts).total_seconds()
        out.update({
            "engine_last_poll_ts": ts.isoformat(),
            "engine_stale_seconds": round(stale, 1),
            "feed_connected": last["feed_connected"],
            "feed_trades": last["feed_trades"],
            "feed_contracts": last["feed_contracts"],
            "as_of": ts.isoformat(),
            "ok": (not in_hours) or stale <= STALE_SECONDS,
        })
        if in_hours and stale > STALE_SECONDS:
            out["detail"] = (f"engine silent for {stale:.0f}s during market "
                             f"hours (limit {STALE_SECONDS}s)")
        return out

    # ── sessions ─────────────────────────────────────────────────────
    def latest(self, underlying: str | None = None) -> dict | None:
        if not self.exists:
            return None
        where, params = _underlying_clause(underlying)
        with _connect(self.path) as con:
            polls = _dicts(con, f"SELECT * FROM poll_snapshot{where} "
                                f"ORDER BY poll_id DESC LIMIT 1", params)
            if not polls:
                return None
            poll = polls[0]
            poll["strikes"] = _dicts(
                con, """SELECT strike, gex, oi_gex, flow_gex, volume, dex FROM poll_strike
                        WHERE poll_id=? ORDER BY strike""", [poll["poll_id"]])
            self._label_strikes(poll["strikes"], poll["spot"])
            poll["position"] = self._open_position(con)
            poll["context"] = self.context(poll, poll["strikes"], con)
        return poll

    @staticmethod
    def _label_strikes(strikes: list[dict], spot: float) -> None:
        """Attach the sign-derived label to EVERY strike, in place.

        The right rail names any strike it lists, not just the annotated
        levels, and spec §0 forbids the UI deriving these itself — so the
        authority (levels.level_label) is applied here once.
        """
        from ..levels import level_label
        for s in strikes:
            s["label"] = level_label(s["gex"], s["strike"] < spot)

    def context(self, poll: dict, strikes: list[dict], con) -> dict:
        """Labels, regime and position text — computed by the ENGINE's own
        functions, never by the UI (spec §0, §2.3).

        `levels.level_label`, `watch.regime_of` and `watch.position_footer`
        are the single authorities for what a level is called, when a regime
        is real, and what the shadow book is doing. A dashboard that
        re-derives any of them will eventually disagree with the Discord
        alert describing the same instant, and the operator will have no way
        to know which is right.
        """
        from ..levels import level_label
        from ..watch import position_footer, regime_context, regime_of

        by_strike = {s["strike"]: s["gex"] for s in strikes}
        spot = poll["spot"]

        def annotate(kind: str, strike: float | None) -> dict | None:
            if strike is None:
                return None
            gex = by_strike.get(strike, 0.0)
            return {
                "kind": kind,
                "strike": strike,
                "gex": gex,
                "label": level_label(gex, strike < spot),
                "distance_pts": strike - spot,
                "distance_pct": (strike - spot) / spot if spot else 0.0,
            }

        levels = [
            a for a in (
                annotate("flip", poll.get("flip")),
                annotate("put_wall", poll.get("put_wall")),
                annotate("call_wall", poll.get("call_wall")),
                annotate("max_accel", poll.get("max_accel")),
                annotate("max_magnet", poll.get("max_magnet")),
                annotate("first_pos_above", poll.get("first_pos_above")),
            ) if a is not None
        ]

        # The dead zone scales with the session's own largest reading, so it
        # has to be measured over the session, not this poll alone.
        peak = con.execute(
            "SELECT max(abs(net_gex)) FROM poll_snapshot WHERE session_date=? "
            "AND underlying=?",
            [poll["session_date"], poll.get("underlying")]).fetchone()[0] or 0.0
        state = regime_of(poll["net_gex"], peak)
        regime = {
            "state": state or "NEUTRAL",
            "called": bool(state),
            "session_peak_abs_net": peak,
            "description": (regime_context(poll["net_gex"]) if state else
                            "Net gamma is inside the dead zone — too small to "
                            "call a regime. This is the absence of a reading, "
                            "not a third regime."),
        }

        # position_footer speaks WatchState; give it one built from the store
        # rather than reading the engine's JSON, so the API stays read-only
        # with respect to the engine's files.
        trades = _dicts(con, "SELECT * FROM shadow_trade WHERE session_date=? "
                             "AND underlying=?",
                        [poll["session_date"], poll.get("underlying")])
        closed = [t for t in trades if t["exit_ts"] is not None]
        open_ = next((t for t in trades if t["exit_ts"] is None), None)
        st = _FooterState(
            open_trade=None if open_ is None else {
                "contracts": open_["contracts"], "strike": open_["strike"],
                "direction": open_["direction"], "expiry": str(open_["expiry"]),
                "entry_premium": open_["entry_premium"],
                "opened_at": str(open_["entry_ts"]),
                "entry_spot": open_["entry_spot"], "level": open_["level"]},
            closed_trades=closed,
            shadow_pnl=sum(t["pnl"] or 0.0 for t in closed),
        )
        prof = {"spot": spot, "net": poll["net_gex"],
                "put_wall": poll.get("put_wall"),
                "call_wall": poll.get("call_wall")}
        footer = position_footer(prof, st)          # type: ignore[arg-type]

        return {"levels": levels, "regime": regime,
                "position_text": footer.replace("**", "").strip(),
                "position": open_}

    def _open_position(self, con) -> dict | None:
        rows = _dicts(con, """SELECT * FROM shadow_trade WHERE exit_ts IS NULL
                              ORDER BY trade_id DESC LIMIT 1""")
        return rows[0] if rows else None

    def polls(self, session_date: dt.date, *, from_minute: int | None = None,
              to_minute: int | None = None,
              underlying: str | None = None) -> list[dict]:
        """Snapshot rows without strike detail — the charting series."""
        if not self.exists:
            return []
        sql = "SELECT * FROM poll_snapshot WHERE session_date=?"
        params: list = [session_date]
        if from_minute is not None:
            sql += " AND minute_of_day >= ?"
            params.append(from_minute)
        if to_minute is not None:
            sql += " AND minute_of_day <= ?"
            params.append(to_minute)
        if underlying != ALL_UNDERLYINGS:
            sql += " AND underlying = ?"
            params.append(underlying or DEFAULT_UNDERLYING)
        with _connect(self.path) as con:
            return _dicts(con, sql + " ORDER BY poll_id", params)

    def profile_at(self, session_date: dt.date, minute: int | None = None,
                   underlying: str | None = None) -> dict | None:
        """The map as it stood at `minute` — or the nearest poll at or before
        it. 'Nearest before', never nearest-either-side: showing a map built
        after the moment asked about would answer a different question than
        the one the user asked.
        """
        if not self.exists:
            return None
        with _connect(self.path) as con:
            sql = "SELECT * FROM poll_snapshot WHERE session_date=?"
            params: list = [session_date]
            if underlying != ALL_UNDERLYINGS:
                sql += " AND underlying = ?"
                params.append(underlying or DEFAULT_UNDERLYING)
            if minute is not None:
                sql += " AND minute_of_day <= ?"
                params.append(minute)
            rows = _dicts(con, sql + " ORDER BY minute_of_day DESC, poll_id DESC "
                                     "LIMIT 1", params)
            if not rows:
                return None
            poll = rows[0]
            poll["strikes"] = _dicts(
                con, """SELECT strike, gex, oi_gex, flow_gex, volume, dex FROM poll_strike
                        WHERE poll_id=? ORDER BY strike""", [poll["poll_id"]])
            poll["requested_minute"] = minute
            self._label_strikes(poll["strikes"], poll["spot"])
            poll["context"] = self.context(poll, poll["strikes"], con)
        return poll

    def expiries(self, poll_id: int) -> list[dict]:
        """Per-expiry rows. `remaining` and `pct` are derived HERE, on read —
        storing cumulative values beside their components creates two numbers
        that can disagree (spec §11.1)."""
        if not self.exists:
            return []
        with _connect(self.path) as con:
            rows = _dicts_optional(
                con, "SELECT * FROM poll_expiry WHERE poll_id=? ORDER BY expiry",
                [poll_id])
        from ..clock import opex_kind
        total = sum(r["gamma"] for r in rows)
        running = total
        for r in rows:
            r["opex"] = opex_kind(r["expiry"])
            running -= r["gamma"]
            r["remaining_after"] = running
            r["pct_of_total"] = (r["gamma"] / total) if total else None
            r["pct_remaining_after"] = (running / total) if total else None
        return rows

    def poll_by_id(self, poll_id: int) -> dict | None:
        if not self.exists:
            return None
        with _connect(self.path) as con:
            rows = _dicts(con, "SELECT * FROM poll_snapshot WHERE poll_id=?",
                          [poll_id])
            if not rows:
                return None
            poll = rows[0]
            poll["strikes"] = _dicts(
                con, """SELECT strike, gex, oi_gex, flow_gex, volume, dex FROM poll_strike
                        WHERE poll_id=? ORDER BY strike""", [poll_id])
            self._label_strikes(poll["strikes"], poll["spot"])
            poll["context"] = self.context(poll, poll["strikes"], con)
        return poll

    def narration(self, poll_id: int) -> dict | None:
        if not self.exists:
            return None
        with _connect(self.path) as con:
            rows = _dicts_optional(
                con, "SELECT * FROM narration WHERE poll_id=?", [poll_id])
        return rows[0] if rows else None

    def previous_poll(self, poll_id: int, underlying: str) -> dict | None:
        if not self.exists:
            return None
        with _connect(self.path) as con:
            rows = _dicts(con, """SELECT * FROM poll_snapshot
                                  WHERE poll_id < ? AND underlying = ?
                                  ORDER BY poll_id DESC LIMIT 1""",
                          [poll_id, underlying])
        return rows[0] if rows else None

    def premium(self, session_date: dt.date) -> list[dict]:
        """Cumulative premium by side through the session (spec §3.3)."""
        if not self.exists:
            return []
        with _connect(self.path) as con:
            return _dicts_optional(
                con, "SELECT * FROM poll_premium WHERE session_date=? "
                     "ORDER BY poll_id", [session_date])

    def strike_matrix(self, session_date: dt.date,
                      underlying: str | None = None) -> dict:
        """Long-form (minute, strike, gex) for the heatmap (spec §3.4).

        Returned long rather than pivoted: strikes drift in and out of the
        window across a session, so a dense matrix would have to invent a
        value for every hole. The client places what exists and leaves the
        rest blank.
        """
        if not self.exists:
            return {"minutes": [], "strikes": [], "cells": []}
        where, params = ("", [session_date])
        if underlying:
            where, params = (" AND p.underlying = ?", [session_date, underlying])
        with _connect(self.path) as con:
            rows = con.execute(
                f"""SELECT p.minute_of_day, s.strike, s.gex
                    FROM poll_strike s JOIN poll_snapshot p USING (poll_id)
                    WHERE p.session_date = ?{where}
                    ORDER BY p.minute_of_day, s.strike""", params).fetchall()
        minutes = sorted({r[0] for r in rows})
        strikes = sorted({r[1] for r in rows})
        return {"minutes": minutes, "strikes": strikes,
                "cells": [[r[0], r[1], r[2]] for r in rows]}

    def alerts(self, session_date: dt.date,
               underlying: str | None = None) -> list[dict]:
        if not self.exists:
            return []
        u = None if underlying == ALL_UNDERLYINGS else (underlying or DEFAULT_UNDERLYING)
        with _connect(self.path) as con:
            if u is None:
                return _dicts(con, """SELECT a.* FROM alert_log a
                                      WHERE CAST(a.ts AS DATE) = ?
                                         OR a.poll_id IN (SELECT poll_id FROM
                                            poll_snapshot WHERE session_date = ?)
                                      ORDER BY a.alert_id""",
                              [session_date, session_date])
            return _dicts(con, """SELECT a.* FROM alert_log a
                                  WHERE a.underlying = ?
                                    AND (CAST(a.ts AS DATE) = ?
                                         OR a.poll_id IN (SELECT poll_id FROM
                                            poll_snapshot WHERE session_date = ?))
                                  ORDER BY a.alert_id""",
                          [u, session_date, session_date])

    def trades(self, session_date: dt.date,
               underlying: str | None = None) -> list[dict]:
        if not self.exists:
            return []
        sql = "SELECT * FROM shadow_trade WHERE session_date=?"
        params: list = [session_date]
        if underlying != ALL_UNDERLYINGS:
            sql += " AND underlying = ?"
            params.append(underlying or DEFAULT_UNDERLYING)
        with _connect(self.path) as con:
            return _dicts(con, sql + " ORDER BY trade_id", params)

    def underlyings(self) -> list[str]:
        if not self.exists:
            return []
        with _connect(self.path) as con:
            return [r[0] for r in con.execute(
                "SELECT DISTINCT underlying FROM poll_snapshot "
                "ORDER BY underlying").fetchall()]

    def sessions(self, limit: int = 30) -> list[dict]:
        if not self.exists:
            return []
        with _connect(self.path) as con:
            return _dicts(con, """
                SELECT p.session_date,
                       count(*)                              AS polls,
                       (SELECT count(*) FROM shadow_trade t
                         WHERE t.session_date = p.session_date) AS trades,
                       (SELECT coalesce(sum(t.pnl), 0) FROM shadow_trade t
                         WHERE t.session_date = p.session_date) AS shadow_pnl,
                       (SELECT count(*) FROM alert_log a
                         WHERE a.poll_id IN (SELECT poll_id FROM poll_snapshot q
                                              WHERE q.session_date = p.session_date))
                                                             AS alerts,
                       min(p.minute_of_day) AS first_minute,
                       max(p.minute_of_day) AS last_minute
                FROM poll_snapshot p
                GROUP BY p.session_date
                ORDER BY p.session_date DESC
                LIMIT ?""", [limit])

    def polls_after(self, poll_id: int) -> list[dict]:
        """Used by the WebSocket fan-out to find what is new."""
        if not self.exists:
            return []
        with _connect(self.path) as con:
            polls = _dicts(con, "SELECT * FROM poll_snapshot WHERE poll_id > ? "
                                "ORDER BY poll_id", [poll_id])
            for p in polls:
                p["strikes"] = _dicts(
                    con, """SELECT strike, gex, oi_gex, flow_gex, volume, dex FROM poll_strike
                            WHERE poll_id=? ORDER BY strike""", [p["poll_id"]])
        return polls

    def alerts_after(self, alert_id: int) -> list[dict]:
        if not self.exists:
            return []
        with _connect(self.path) as con:
            return _dicts(con, "SELECT * FROM alert_log WHERE alert_id > ? "
                               "ORDER BY alert_id", [alert_id])

    def trades_changed_after(self, stamp: int) -> list[dict]:
        """Trades opened or closed since `stamp` (a trade_id watermark).

        Closes do not create a new row, so a trade_id watermark alone would
        miss them; the exit timestamp is checked as well.
        """
        if not self.exists:
            return []
        with _connect(self.path) as con:
            return _dicts(con, """SELECT * FROM shadow_trade
                                  WHERE trade_id > ? OR exit_ts IS NOT NULL
                                  ORDER BY trade_id""", [stamp])

    def live_tick(self, last_poll: int, last_alert: int,
                  underlying: str | None = None) -> dict:
        """Everything the WebSocket needs for one tick, on ONE connection.

        Four separate reader calls per second kept a read lock open almost
        continuously and starved the engine's writes. Batching them cuts the
        connection churn fourfold and leaves the file free between ticks.
        """
        if not self.exists:
            return {"polls": [], "alerts": [], "trades": []}
        u = None if underlying == ALL_UNDERLYINGS else (underlying or DEFAULT_UNDERLYING)
        with _connect(self.path) as con:
            # Filtered: with SPY and COMPLEX writing in parallel, an
            # unfiltered stream would flip the screen between instruments
            # mid-session without the operator asking for it.
            polls = _dicts(con,
                           "SELECT * FROM poll_snapshot WHERE poll_id > ?"
                           + ("" if u is None else " AND underlying = ?")
                           + " ORDER BY poll_id",
                           [last_poll] + ([] if u is None else [u]))
            for p in polls:
                p["strikes"] = _dicts(
                    con, """SELECT strike, gex, oi_gex, flow_gex, volume, dex FROM poll_strike
                            WHERE poll_id=? ORDER BY strike""", [p["poll_id"]])
                # A pushed poll is self-describing: carrying the previous
                # poll's levels and regime alongside a new spot would put two
                # different instants on one screen.
                self._label_strikes(p["strikes"], p["spot"])
                p["context"] = self.context(p, p["strikes"], con)
                p["position"] = self._open_position(con)
                p["as_of"] = p["ts"]
            alerts = _dicts(con,
                            "SELECT * FROM alert_log WHERE alert_id > ?"
                            + ("" if u is None else " AND underlying = ?")
                            + " ORDER BY alert_id",
                            [last_alert] + ([] if u is None else [u]))
            trades = _dicts(con, "SELECT * FROM shadow_trade"
                            + ("" if u is None else " WHERE underlying = ?")
                            + " ORDER BY trade_id", [] if u is None else [u])
        return {"polls": polls, "alerts": alerts, "trades": trades}

    def max_ids(self, underlying: str | None = None) -> tuple[int, int]:
        if not self.exists:
            return (0, 0)
        u = None if underlying == ALL_UNDERLYINGS else (underlying or DEFAULT_UNDERLYING)
        with _connect(self.path) as con:
            p = con.execute("SELECT coalesce(max(poll_id), 0) FROM poll_snapshot"
                            + ("" if u is None else " WHERE underlying = ?"),
                            [] if u is None else [u]).fetchone()[0]
            a = con.execute("SELECT coalesce(max(alert_id), 0) FROM alert_log"
                            + ("" if u is None else " WHERE underlying = ?"),
                            [] if u is None else [u]).fetchone()[0]
        return (int(p), int(a))


# ── backtest bundles on disk ─────────────────────────────────────────
class BundleReader:
    """Reads the results bundles `backtest.py` already writes. Never re-runs
    anything (spec §4.1) — running a backtest from an HTTP request would put
    a strategy evaluation behind a URL, which is exactly the sort of casual
    re-running the pre-registration exists to prevent.
    """

    def __init__(self, root: Path | str | None = None):
        self.root = Path(root or (Path(settings.gex_data_root) / "results"))

    def list(self) -> list[dict]:
        if not self.root.exists():
            return []
        out = []
        for d in sorted(self.root.iterdir(), reverse=True):
            if not d.is_dir():
                continue
            gates = d / "gates.json"
            entry = {"name": d.name, "has_gates": gates.exists(),
                     "modified": dt.datetime.fromtimestamp(
                         d.stat().st_mtime, tz=dt.timezone.utc).isoformat()}
            if gates.exists():
                try:
                    blob = json.loads(gates.read_text())
                    g = blob.get("gates", blob)
                    entry["gate_params"] = blob.get("gate_params")
                    entry["go_live_eligible"] = bool(
                        (g.get("GO_LIVE_ELIGIBLE") or {}).get("passed"))
                except (OSError, json.JSONDecodeError):
                    entry["has_gates"] = False
            out.append(entry)
        return out

    def get(self, name: str) -> dict | None:
        """`name` is resolved strictly against the bundle directory; a path
        that escapes it is refused rather than followed."""
        d = (self.root / name).resolve()
        if not str(d).startswith(str(self.root.resolve())) or not d.is_dir():
            return None
        out: dict = {"name": d.name, "gates": None, "gate_params": None,
                     "mark_provenance": None, "summary_md": None,
                     "days": [], "trades": []}
        gates = d / "gates.json"
        if gates.exists():
            blob = json.loads(gates.read_text())
            out["gates"] = blob.get("gates", blob)
            out["gate_params"] = blob.get("gate_params")
            # None means the bundle predates provenance recording — shown as
            # "not recorded", never as 0% fallback.
            out["mark_provenance"] = blob.get("mark_provenance")
        summary = d / "summary.md"
        if summary.exists():
            out["summary_md"] = summary.read_text()
        for key, fn in (("days", "days.parquet"), ("trades", "trades.parquet")):
            p = d / fn
            if p.exists():
                import polars as pl
                out[key] = pl.read_parquet(p).to_dicts()
        return out


def _version() -> str:
    try:
        from importlib.metadata import version
        return version("gexbot")
    except Exception:
        return "unknown"
