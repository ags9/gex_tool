"""Phase 2: the read-only API.

The tests that matter here are the last three: that the API cannot lock out
the engine, that it cannot be talked into writing anything, and that it
reports staleness honestly instead of serving old numbers as current.
"""
import datetime as dt
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gexbot.api.app import create_app
from gexbot.api.reader import StateReader
from gexbot.state import StateStore

PROFILE = {
    "spot": 7600.0, "net": -1.25e9, "flip": 7582.5,
    "put_wall": 7550.0, "call_wall": 7650.0,
    "max_accel": 7550.0, "max_magnet": 7650.0,
    "first_positive_above": 7620.0, "expiries": ["2026-09-11"],
    "by_strike": {7550.0: -8.0e8, 7600.0: -2.0e8, 7650.0: 4.0e8},
}
DAY = dt.date(2026, 6, 10)


def engine_process(db: Path, body: str) -> str:
    """Run a StateStore write in ANOTHER interpreter.

    This is the production topology and the only one worth testing: DuckDB
    permits one writing process or many reading ones, enforced by a file
    lock, and forbids mixing read-only and read-write connections *inside* a
    single process outright. A same-process test would exercise a
    constraint the deployed system never meets and miss the lock path that
    it does.
    """
    # dedent the body on its own: interpolating it into an indented template
    # and dedenting afterwards leaves the template's indent behind, because
    # the injected lines have no common prefix to strip.
    code = (f"from gexbot.state import StateStore\n"
            f"store = StateStore({str(db)!r})\n"
            + textwrap.dedent(body))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, timeout=120, check=False)
    assert r.returncode == 0, f"engine process failed: {r.stderr}"
    return r.stdout.strip()


def _ts(minute: int) -> dt.datetime:
    """A UTC instant that is `minute` ET on DAY."""
    from gexbot.clock import et_minute_to_utc_ns
    return dt.datetime.fromtimestamp(
        et_minute_to_utc_ns(DAY, minute) / 1e9, tz=dt.timezone.utc)


@pytest.fixture()
def populated(tmp_path: Path):
    store = StateStore(tmp_path / "state.duckdb")
    pids = []
    for minute in (10 * 60, 11 * 60, 12 * 60):
        pid = store.write_poll(dict(PROFILE, spot=7600.0 + minute / 60),
                               {"connected": True, "trades": 5, "contracts": 50},
                               120, ts=_ts(minute))
        pids.append(pid)
        store.write_alert(channel="alerts", kind="proximity", title=f"m{minute}",
                          body="b", poll_id=pid, spot=7600.0, strike=7550.0,
                          ts=_ts(minute))
    tid = store.open_shadow_trade(
        direction=-1, strike=7550.0, expiry=dt.date(2026, 6, 12), contracts=2,
        trigger="t", level=7550.0, entry_spot=7545.0, entry_premium=12.0,
        ts=_ts(10 * 60))
    return store, pids, tid, tmp_path


@pytest.fixture()
def client(populated):
    store, _, _, tmp_path = populated
    return TestClient(create_app(store.path, results_root=tmp_path / "results"))


# ── REST ─────────────────────────────────────────────────────────────
def test_health_reports_the_engine(client):
    h = client.get("/api/health").json()
    assert h["engine_last_poll_ts"] and h["engine_stale_seconds"] is not None
    assert h["feed_connected"] is True and h["feed_trades"] == 5


def test_health_is_honest_when_there_is_no_store(tmp_path: Path):
    c = TestClient(create_app(tmp_path / "absent.duckdb"))
    h = c.get("/api/health").json()
    assert h["ok"] is False and "never run" in h["detail"]


def test_health_flags_a_silent_engine_during_market_hours(populated, monkeypatch):
    """Spec §4.3: >5 minutes without a poll in-hours is not ok. Outside
    hours the same silence is correct and must not be flagged."""
    store, _, _, _ = populated
    reader = StateReader(store.path)

    in_hours = _ts(11 * 60) + dt.timedelta(hours=2)      # 13:00 ET, 1h stale
    assert reader.health(now=in_hours)["ok"] is False
    assert "silent" in reader.health(now=in_hours)["detail"]

    after_close = _ts(12 * 60) + dt.timedelta(hours=8)   # 20:00 ET
    assert reader.health(now=after_close)["ok"] is True


def test_latest_carries_strikes_position_and_as_of(client, populated):
    _, pids, tid, _ = populated
    body = client.get("/api/session/latest").json()
    assert body["poll_id"] == pids[-1]
    assert len(body["strikes"]) == 3
    assert body["position"]["trade_id"] == tid, "open shadow trade is surfaced"
    assert body["as_of"] == body["ts"]


def test_polls_filter_by_minute(client):
    d = DAY.isoformat()
    assert len(client.get(f"/api/session/{d}/polls").json()) == 3
    got = client.get(f"/api/session/{d}/polls?from_minute=660").json()
    assert [p["minute_of_day"] for p in got] == [660, 720]


def test_profile_returns_nearest_at_or_before(client):
    """'What did the map look like at 11:40?' must answer with the map that
    existed then — never one built afterwards."""
    d = DAY.isoformat()
    body = client.get(f"/api/session/{d}/profile?minute=700").json()
    assert body["minute_of_day"] == 660
    assert body["requested_minute"] == 700
    assert len(body["strikes"]) == 3
    assert client.get(f"/api/session/{d}/profile?minute=540").status_code == 404


def test_alerts_and_trades_and_sessions(client, populated):
    tid = populated[2]
    d = DAY.isoformat()
    assert len(client.get(f"/api/session/{d}/alerts").json()) == 3
    trades = client.get(f"/api/session/{d}/trades").json()
    assert len(trades) == 1 and trades[0]["trade_id"] == tid
    s, = client.get("/api/sessions").json()
    assert s["polls"] == 3 and s["alerts"] == 3 and s["trades"] == 1


def test_bad_date_is_a_400_not_a_500(client):
    assert client.get("/api/session/not-a-date/polls").status_code == 400


def test_every_response_carries_as_of_header(client):
    for path in ("/api/health", "/api/sessions",
                 f"/api/session/{DAY.isoformat()}/polls"):
        assert client.get(path).headers.get("X-As-Of")


# ── backtest bundles ─────────────────────────────────────────────────
def test_bundles_are_listed_and_read(tmp_path: Path, populated):
    import json
    store = populated[0]
    root = tmp_path / "results"
    (root / "20260101_bundle").mkdir(parents=True)
    (root / "20260101_bundle" / "gates.json").write_text(json.dumps({
        "gate_params": {"max_drawdown_frac": 0.20},
        "gates": {"GO_LIVE_ELIGIBLE": {"passed": False, "detail": "d"}}}))
    (root / "20260101_bundle" / "summary.md").write_text("# hi")

    c = TestClient(create_app(store.path, results_root=root))
    listed = c.get("/api/backtests").json()
    assert listed[0]["name"] == "20260101_bundle"
    assert listed[0]["gate_params"]["max_drawdown_frac"] == 0.20
    assert listed[0]["go_live_eligible"] is False

    one = c.get("/api/backtests/20260101_bundle").json()
    assert one["summary_md"] == "# hi"
    assert c.get("/api/backtests/nope").status_code == 404


def test_bundle_path_traversal_is_refused(tmp_path: Path, populated):
    store, _, _, _ = populated
    c = TestClient(create_app(store.path, results_root=tmp_path / "results"))
    assert c.get("/api/backtests/..%2f..%2fetc").status_code == 404


# ── the three that matter ────────────────────────────────────────────
def test_api_is_read_only(client):
    """No verb other than GET is served. The engine cannot be driven from
    here, and neither can a parameter be changed."""
    d = DAY.isoformat()
    for method, path in (("post", "/api/health"), ("put", "/api/sessions"),
                         ("delete", f"/api/session/{d}/trades"),
                         ("patch", "/api/session/latest")):
        assert getattr(client, method)(path).status_code in (404, 405)


def test_api_reads_do_not_lock_out_the_engine(populated):
    """Spec §5: engine and API running together must produce no lock errors.

    The engine writes from its own process while the API is hammered with
    reads from this one. Without the retry in `state.connect` the two collide
    — DuckDB's file lock is exclusive for the milliseconds a write takes, and
    the WebSocket loop alone reads several times a second.
    """
    store, _, _, _ = populated
    c = TestClient(create_app(store.path))
    errors: list = []
    stop = threading.Event()

    def hammer_reads():
        try:
            while not stop.is_set():
                assert c.get("/api/health").status_code == 200
                assert c.get("/api/session/latest").status_code == 200
        except Exception as e:                  # noqa: BLE001 - reported below
            errors.append(e)

    t = threading.Thread(target=hammer_reads)
    t.start()
    try:
        written = engine_process(store.path, textwrap.dedent("""
            ok = 0
            for i in range(30):
                pid = store.write_poll({'spot': 7600.0 + i, 'net': -1.0e9,
                                        'by_strike': {7550.0: -1.0e8},
                                        'expiries': ['2026-06-12']})
                ok += pid is not None
            print(f'{ok} {store.write_errors}')
        """))
    finally:
        stop.set()
        t.join(timeout=30)

    ok, write_errors = (int(x) for x in written.split())
    assert ok == 30, f"engine lost {30 - ok} writes to lock contention"
    assert write_errors == 0
    assert not errors, f"reads failed while the engine wrote: {errors}"


def test_websocket_sends_snapshot_then_streams_new_polls(populated):
    store, pids, _, _ = populated
    c = TestClient(create_app(store.path))
    with c.websocket_connect("/ws/live") as ws:
        first = ws.receive_json()
        assert first["type"] == "snapshot"
        assert first["data"]["poll_id"] == pids[-1]
        assert first["data"]["as_of"]

        new_pid = int(engine_process(store.path,
            "print(store.write_poll({'spot': 7777.0, 'net': -1.0e9,"
            " 'by_strike': {7550.0: -1.0e8}, 'expiries': ['2026-06-12']}))"))
        seen = {}
        for _ in range(12):                     # drain until the poll shows up
            msg = ws.receive_json()
            seen.setdefault(msg["type"], msg)
            if msg["type"] == "poll":
                break
        assert "poll" in seen, f"never received the new poll; saw {list(seen)}"
        assert seen["poll"]["data"]["poll_id"] == new_pid
        assert seen["poll"]["data"]["spot"] == 7777.0
        assert seen["poll"]["data"]["strikes"]


def test_websocket_reports_a_shadow_exit(populated):
    """A close updates a row rather than inserting one, so an id watermark
    alone would never report an exit."""
    store, _, tid, _ = populated
    c = TestClient(create_app(store.path))
    with c.websocket_connect("/ws/live") as ws:
        assert ws.receive_json()["type"] == "snapshot"
        engine_process(store.path,
            f"store.close_shadow_trade({tid}, exit_spot=7500.0, "
            f"exit_premium=20.0, exit_reason='stop', pnl=1600.0)")
        for _ in range(15):
            msg = ws.receive_json()
            if msg["type"] == "trade" and msg["data"]["exit_reason"]:
                assert msg["data"]["pnl"] == 1600.0
                return
        pytest.fail("exit never streamed")
