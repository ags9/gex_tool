"""Phase 1: the state store, and watch.py's wiring into it.

The load-bearing test here is the last one. Everything else checks that data
survives a round trip; that one checks that a broken store cannot take the
engine down with it, which is the property the spec actually cares about
(§2.3: losing a poll row is acceptable, missing a breaker alert is not).
"""
import datetime as dt
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

from gexbot.notify import Channel, Color
from gexbot.state import StateStore
from gexbot.watch import AlertSink, WatchState, do_shadow

PROFILE = {
    "spot": 7600.0,
    "net": -1.25e9,
    "flip": 7582.5,
    "put_wall": 7550.0,
    "call_wall": 7650.0,
    "max_accel": 7550.0,
    "max_magnet": 7650.0,
    "first_positive_above": 7620.0,
    "expiries": ["2026-09-11", "2026-09-14"],
    "by_strike": {7550.0: -8.0e8, 7600.0: -2.0e8, 7650.0: 4.0e8},
}


@pytest.fixture()
def store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "state.duckdb")


def _rows(store: StateStore, sql: str) -> list[tuple]:
    with duckdb.connect(store.path, read_only=True) as con:
        return con.execute(sql).fetchall()


# ── schema ───────────────────────────────────────────────────────────
def test_schema_creation_is_idempotent(tmp_path: Path):
    p = tmp_path / "state.duckdb"
    first = StateStore(p)
    first.write_poll(PROFILE)
    second = StateStore(p)                     # re-open, re-run CREATE IF NOT EXISTS
    assert second.available
    assert len(_rows(second, "SELECT * FROM poll_snapshot")) == 1, "data survived"
    assert second.write_poll(PROFILE) is not None
    assert len(_rows(second, "SELECT * FROM poll_snapshot")) == 2


def test_engine_holds_no_lock_between_writes(store: StateStore):
    """Connection-per-write is the whole point: a held write handle locks out
    the API and every notebook.

    Checked from another process, because that is the only place it is
    checkable. DuckDB refuses to mix read-only and read-write connections
    within a single interpreter at all, so an in-process version of this test
    would assert a constraint the deployed system never meets — and, if its
    write result went unchecked, would pass while proving nothing.
    """
    store.write_poll(PROFILE)
    code = (f"import duckdb\n"
            f"con = duckdb.connect({store.path!r}, read_only=True)\n"
            f"print(con.execute('SELECT count(*) FROM poll_snapshot').fetchone()[0])")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, check=False)
    assert r.returncode == 0, f"reader locked out: {r.stderr}"
    assert r.stdout.strip() == "1"

    # and the engine keeps writing afterwards, with nothing swallowed
    assert store.write_poll(PROFILE) is not None
    assert store.write_errors == 0


# ── poll round trip ──────────────────────────────────────────────────
def test_poll_round_trip(store: StateStore):
    ts = dt.datetime(2026, 6, 10, 15, 40, tzinfo=dt.timezone.utc)   # 11:40 ET
    pid = store.write_poll(PROFILE, {"connected": True, "trades": 12,
                                     "contracts": 340}, 250, ts=ts)
    assert pid == int(ts.timestamp() * 1000)

    row, = _rows(store, """SELECT session_date, minute_of_day, spot, net_gex,
                                  flip, put_wall, call_wall, expiries, model,
                                  per_point, feed_connected, feed_trades,
                                  feed_contracts, poll_ms
                           FROM poll_snapshot""")
    assert row[0] == dt.date(2026, 6, 10)
    assert row[1] == 11 * 60 + 40, "minute_of_day is ET, not UTC"
    assert (row[2], row[3]) == (7600.0, -1.25e9)
    assert row[7] == "2026-09-11,2026-09-14"
    assert (row[8], row[9]) == ("naive", True)
    assert (row[10], row[11], row[12], row[13]) == (True, 12, 340, 250)

    strikes = _rows(store, "SELECT strike, gex, oi_gex, flow_gex "
                           "FROM poll_strike ORDER BY strike")
    assert [(s, g) for s, g, _, _ in strikes] == sorted(PROFILE["by_strike"].items())
    assert all(oi is None and fl is None for _, _, oi, fl in strikes), \
        "no flow overlay: the split is NULL, not zero"


def test_session_date_is_et_not_utc(store: StateStore):
    """20:30 UTC on the 10th is 16:30 ET the same day; 01:00 UTC on the 11th
    is 21:00 ET on the 10th. A UTC-derived date files the latter wrongly."""
    late = dt.datetime(2026, 6, 11, 1, 0, tzinfo=dt.timezone.utc)
    store.write_poll(PROFILE, ts=late)
    assert _rows(store, "SELECT session_date FROM poll_snapshot")[0][0] \
        == dt.date(2026, 6, 10)


def test_flow_overlay_split_is_recorded_when_present(store: StateStore):
    prof = dict(PROFILE, oi_net=-1.0e9, flow_net=-2.5e8,
                oi_by_strike={7550.0: -7.0e8}, flow_by_strike={7550.0: -1.0e8})
    store.write_poll(prof)
    assert _rows(store, "SELECT oi_net, flow_net FROM poll_snapshot")[0] \
        == (-1.0e9, -2.5e8)
    assert _rows(store, "SELECT oi_gex, flow_gex FROM poll_strike "
                        "WHERE strike=7550.0")[0] == (-7.0e8, -1.0e8)


def test_poll_ids_do_not_collide_within_a_millisecond(store: StateStore):
    ts = dt.datetime(2026, 6, 10, 15, 40, tzinfo=dt.timezone.utc)
    ids = [store.write_poll(PROFILE, ts=ts) for _ in range(5)]
    assert len(set(ids)) == 5 and ids == sorted(ids)


# ── alert round trip ─────────────────────────────────────────────────
def test_alert_round_trip(store: StateStore):
    pid = store.write_poll(PROFILE)
    store.write_alert(channel="alerts", kind="proximity",
                      title="Spot approaching 7,550 — TRAPDOOR",
                      body="body text", poll_id=pid, spot=7552.0, strike=7550.0)
    row, = _rows(store, "SELECT poll_id, channel, kind, title, spot, strike "
                        "FROM alert_log")
    assert row == (pid, "alerts", "proximity",
                   "Spot approaching 7,550 — TRAPDOOR", 7552.0, 7550.0)


# ── shadow trade round trip ──────────────────────────────────────────
def test_shadow_trade_open_then_close(store: StateStore):
    tid = store.open_shadow_trade(
        direction=-1, strike=7550.0, expiry=dt.date(2026, 9, 16), contracts=2,
        trigger="spot below put wall", level=7550.0, entry_spot=7545.0,
        entry_premium=12.5)

    row, = _rows(store, "SELECT direction, contracts, exit_ts, pnl FROM shadow_trade")
    assert row[:2] == (-1, 2)
    assert row[2] is None and row[3] is None, "open trade has null exit columns"

    assert store.close_shadow_trade(tid, exit_spot=7500.0, exit_premium=20.0,
                                    exit_reason="stop (-40% premium)", pnl=1500.0)
    row, = _rows(store, "SELECT exit_spot, exit_premium, exit_reason, pnl, "
                        "exit_minute FROM shadow_trade")
    assert row[:4] == (7500.0, 20.0, "stop (-40% premium)", 1500.0)
    assert row[4] is not None


def test_close_without_a_trade_id_is_refused_not_guessed(store: StateStore):
    """A trade opened before the store existed has no id. Closing 'whatever
    is open' would silently corrupt an unrelated row."""
    store.open_shadow_trade(direction=1, strike=7600.0,
                            expiry=dt.date(2026, 9, 16), contracts=1,
                            trigger="t", level=7600.0, entry_spot=7600.0,
                            entry_premium=10.0)
    assert store.close_shadow_trade(None, exit_spot=1.0, exit_premium=1.0,
                                    exit_reason="x", pnl=1.0) is False
    assert _rows(store, "SELECT pnl FROM shadow_trade")[0][0] is None


# ── the one that matters: failure never reaches the engine ───────────
def test_write_failure_does_not_propagate(store: StateStore, monkeypatch):
    """Spec §2.3. Every writer must swallow, count, and continue — a state
    problem must never suppress an alert or kill the poll loop."""
    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr("gexbot.state.duckdb.connect", boom)

    assert store.write_poll(PROFILE) is None
    assert store.write_alert(channel="alerts", kind="map", title="t", body="b") is None
    assert store.open_shadow_trade(
        direction=1, strike=1.0, expiry=dt.date(2026, 9, 16), contracts=1,
        trigger="t", level=None, entry_spot=1.0, entry_premium=1.0) is None
    assert store.close_shadow_trade(1, exit_spot=1.0, exit_premium=1.0,
                                    exit_reason="x", pnl=0.0) is False

    assert store.write_errors == 4
    assert "disk on fire" in store.last_error


def test_unavailable_store_is_inert_not_fatal(tmp_path: Path, monkeypatch):
    """If even schema creation fails the engine still runs, unrecorded."""
    monkeypatch.setattr("gexbot.state.duckdb.connect",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no disk")))
    s = StateStore(tmp_path / "state.duckdb")
    assert s.available is False
    assert s.write_poll(PROFILE) is None
    assert s.write_alert(channel="alerts", kind="map", title="t", body="b") is None


# ── watch.py wiring ──────────────────────────────────────────────────
class _FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, channel, title, body, color=Color.BLUE, fields=None):
        self.sent.append((channel, title))

    def daily_digest(self, *, body, green_day):
        self.sent.append((Channel.DAILY, "Daily digest"))

    def flush(self, timeout=15.0):
        pass


def test_sink_records_every_alert_it_sends(store: StateStore):
    """The notifier and the store must never disagree (§3.2). One call site
    is what guarantees it."""
    n = _FakeNotifier()
    sink = AlertSink(n, store)
    sink.poll_id = store.write_poll(PROFILE)

    sink.send(Channel.ALERTS, "Regime flip", "b", Color.AMBER,
              kind="regime_flip", spot=7600.0)
    sink.send(Channel.TRADES, "would BUY", "b", kind="shadow_entry",
              spot=7600.0, strike=7550.0)
    sink.daily_digest(body="summary", green_day=True)

    assert len(n.sent) == 3
    rows = _rows(store, "SELECT kind, channel, poll_id FROM alert_log ORDER BY alert_id")
    assert [r[0] for r in rows] == ["regime_flip", "shadow_entry", "digest"]
    assert [r[1] for r in rows] == ["alerts", "trades", "daily"]
    assert all(r[2] == sink.poll_id for r in rows), "alerts point at their poll"


def test_sink_still_sends_when_the_store_is_broken(store: StateStore, monkeypatch):
    """The asymmetry the spec insists on: the alert goes out even if nothing
    can be recorded."""
    monkeypatch.setattr("gexbot.state.duckdb.connect",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    n = _FakeNotifier()
    AlertSink(n, store).send(Channel.ALERTS, "breaker", "b", kind="warning")
    assert len(n.sent) == 1
    assert store.write_errors == 1


def test_do_shadow_records_the_trade_it_narrates(store: StateStore, monkeypatch):
    """Entry and exit both land in shadow_trade, linked by trade_id carried
    through the JSON state."""
    monkeypatch.setattr("gexbot.watch.contract_price",
                        lambda *a, **k: (10.0, 0.5))
    monkeypatch.setattr("gexbot.watch.minute_now", lambda: 11 * 60)

    n = _FakeNotifier()
    sink = AlertSink(n, store)
    st = WatchState(day="2026-06-10")
    prof = dict(PROFILE, spot=7540.0)          # below the put wall, net < 0

    do_shadow(sink, prof, st, "key", "I:SPX", 3000.0, store)
    assert st.open_trade is not None
    tid = st.open_trade["trade_id"]
    assert tid is not None
    assert _rows(store, "SELECT direction, exit_ts FROM shadow_trade")[0] == (-1, None)

    # drive it to the EOD exit
    monkeypatch.setattr("gexbot.watch.minute_now", lambda: 15 * 60 + 55)
    do_shadow(sink, prof, st, "key", "I:SPX", 3000.0, store)
    assert st.open_trade is None
    row, = _rows(store, "SELECT trade_id, exit_reason, pnl FROM shadow_trade")
    assert row[0] == tid and row[1] and row[2] is not None

    kinds = [r[0] for r in _rows(store, "SELECT kind FROM alert_log ORDER BY alert_id")]
    assert kinds == ["shadow_entry", "shadow_exit"]


def test_do_shadow_without_a_store_still_narrates(store: StateStore, monkeypatch):
    """store=None is the pre-Phase-1 behaviour and must keep working."""
    monkeypatch.setattr("gexbot.watch.contract_price", lambda *a, **k: (10.0, 0.5))
    monkeypatch.setattr("gexbot.watch.minute_now", lambda: 11 * 60)
    n = _FakeNotifier()
    st = WatchState(day="2026-06-10")
    do_shadow(AlertSink(n, store), dict(PROFILE, spot=7540.0), st,
              "key", "I:SPX", 3000.0, None)
    assert st.open_trade is not None and st.open_trade["trade_id"] is None
    assert len(n.sent) == 1


def test_watch_once_writes_exactly_one_poll(tmp_path: Path, monkeypatch):
    """Spec §5 acceptance, offline: one --once run produces one poll_snapshot
    row, its strikes, and the alerts it sent — no more, no less.

    Network and Discord are both stubbed: no webhook URLs are configured, so
    DiscordNotifier drops every message rather than posting anywhere.
    """
    from gexbot import watch as w

    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    for var in ("DISCORD_WEBHOOK_TRADES", "DISCORD_WEBHOOK_ALERTS",
                "DISCORD_WEBHOOK_DAILY"):
        monkeypatch.delenv(var, raising=False)

    monkeypatch.setattr(w.settings, "gex_data_root", tmp_path)
    monkeypatch.setattr(w, "fetch_index_spot", lambda *a, **k: 7600.0)
    monkeypatch.setattr(w, "fetch_chain", lambda *a, **k: ([], 7600.0))
    monkeypatch.setattr(w, "build_profile", lambda *a, **k: dict(PROFILE))
    monkeypatch.setattr(w, "contract_price", lambda *a, **k: (10.0, 0.5))
    monkeypatch.setattr(w, "minute_now", lambda: 11 * 60)

    db = tmp_path / "state.duckdb"
    w.run_watch(once=True, shadow=True, state_db=db)

    s = StateStore(db)
    polls = _rows(s, "SELECT poll_id, spot, net_gex, poll_ms FROM poll_snapshot")
    assert len(polls) == 1
    pid, spot, net, poll_ms = polls[0]
    assert (spot, net) == (7600.0, -1.25e9)
    assert poll_ms is not None and poll_ms >= 0

    assert len(_rows(s, "SELECT * FROM poll_strike")) == len(PROFILE["by_strike"])
    alerts = _rows(s, "SELECT kind, poll_id FROM alert_log")
    assert alerts, "the opening map alert at minimum"
    assert all(a[1] == pid for a in alerts), "every alert links to its poll"
