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
from pathlib import Path

from ..clock import ET
from ..config import settings
from ..state import connect, default_path

# Market hours in ET minutes-of-day, used only to decide whether silence from
# the engine is alarming or expected.
SESSION_OPEN = 9 * 60 + 30
SESSION_CLOSE = 16 * 60
STALE_SECONDS = 300          # spec §4.3: >5 min without a poll in-hours = not ok


def _connect(path: Path | str):
    """Read-only, and retrying: the engine holds the write lock for a few
    milliseconds per poll, and a read that lands inside that window must
    wait rather than 500."""
    return connect(str(path), read_only=True)


def _dicts(con, sql: str, params: list | None = None) -> list[dict]:
    cur = con.execute(sql, params or [])
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


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
    def latest(self) -> dict | None:
        if not self.exists:
            return None
        with _connect(self.path) as con:
            polls = _dicts(con, "SELECT * FROM poll_snapshot "
                                "ORDER BY poll_id DESC LIMIT 1")
            if not polls:
                return None
            poll = polls[0]
            poll["strikes"] = _dicts(
                con, """SELECT strike, gex, oi_gex, flow_gex FROM poll_strike
                        WHERE poll_id=? ORDER BY strike""", [poll["poll_id"]])
            poll["position"] = self._open_position(con)
        return poll

    def _open_position(self, con) -> dict | None:
        rows = _dicts(con, """SELECT * FROM shadow_trade WHERE exit_ts IS NULL
                              ORDER BY trade_id DESC LIMIT 1""")
        return rows[0] if rows else None

    def polls(self, session_date: dt.date, *, from_minute: int | None = None,
              to_minute: int | None = None) -> list[dict]:
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
        with _connect(self.path) as con:
            return _dicts(con, sql + " ORDER BY poll_id", params)

    def profile_at(self, session_date: dt.date, minute: int | None = None) -> dict | None:
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
            if minute is not None:
                sql += " AND minute_of_day <= ?"
                params.append(minute)
            rows = _dicts(con, sql + " ORDER BY minute_of_day DESC, poll_id DESC "
                                     "LIMIT 1", params)
            if not rows:
                return None
            poll = rows[0]
            poll["strikes"] = _dicts(
                con, """SELECT strike, gex, oi_gex, flow_gex FROM poll_strike
                        WHERE poll_id=? ORDER BY strike""", [poll["poll_id"]])
            poll["requested_minute"] = minute
        return poll

    def alerts(self, session_date: dt.date) -> list[dict]:
        if not self.exists:
            return []
        with _connect(self.path) as con:
            return _dicts(con, """SELECT a.* FROM alert_log a
                                  WHERE CAST(a.ts AS DATE) = ?
                                     OR a.poll_id IN (SELECT poll_id FROM
                                        poll_snapshot WHERE session_date = ?)
                                  ORDER BY a.alert_id""",
                          [session_date, session_date])

    def trades(self, session_date: dt.date) -> list[dict]:
        if not self.exists:
            return []
        with _connect(self.path) as con:
            return _dicts(con, "SELECT * FROM shadow_trade WHERE session_date=? "
                               "ORDER BY trade_id", [session_date])

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
                    con, """SELECT strike, gex, oi_gex, flow_gex FROM poll_strike
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

    def live_tick(self, last_poll: int, last_alert: int) -> dict:
        """Everything the WebSocket needs for one tick, on ONE connection.

        Four separate reader calls per second kept a read lock open almost
        continuously and starved the engine's writes. Batching them cuts the
        connection churn fourfold and leaves the file free between ticks.
        """
        if not self.exists:
            return {"polls": [], "alerts": [], "trades": []}
        with _connect(self.path) as con:
            polls = _dicts(con, "SELECT * FROM poll_snapshot WHERE poll_id > ? "
                                "ORDER BY poll_id", [last_poll])
            for p in polls:
                p["strikes"] = _dicts(
                    con, """SELECT strike, gex, oi_gex, flow_gex FROM poll_strike
                            WHERE poll_id=? ORDER BY strike""", [p["poll_id"]])
            alerts = _dicts(con, "SELECT * FROM alert_log WHERE alert_id > ? "
                                 "ORDER BY alert_id", [last_alert])
            trades = _dicts(con, "SELECT * FROM shadow_trade ORDER BY trade_id")
        return {"polls": polls, "alerts": alerts, "trades": trades}

    def max_ids(self) -> tuple[int, int]:
        if not self.exists:
            return (0, 0)
        with _connect(self.path) as con:
            p = con.execute("SELECT coalesce(max(poll_id), 0) "
                            "FROM poll_snapshot").fetchone()[0]
            a = con.execute("SELECT coalesce(max(alert_id), 0) "
                            "FROM alert_log").fetchone()[0]
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
                     "summary_md": None, "days": [], "trades": []}
        gates = d / "gates.json"
        if gates.exists():
            blob = json.loads(gates.read_text())
            out["gates"] = blob.get("gates", blob)
            out["gate_params"] = blob.get("gate_params")
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
