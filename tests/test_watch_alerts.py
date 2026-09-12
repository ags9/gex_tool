"""Structural alert discipline: the footer, the regime dead zone, the window.

All three exist because an alert that fires on noise is worse than no alert.
A phone that buzzes for a regime change that did not happen trains you to
ignore it, and the one that matters arrives into that trained indifference.
"""
import datetime as dt
from pathlib import Path

import pytest

from gexbot.notify import Channel, Color
from gexbot.state import StateStore
from gexbot.watch import (
    ALERT_WINDOW_END,
    ALERT_WINDOW_START,
    REGIME_MIN_ABS_GEX,
    AlertSink,
    WatchState,
    in_alert_window,
    post_map,
    regime_of,
    structural_alerts,
)

FOOTER_MARK = "**📄 Shadow position:**"


def profile(net: float, spot: float = 7600.0, flip=7582.5) -> dict:
    return {
        "spot": spot, "net": net, "flip": flip,
        "put_wall": 7550.0, "call_wall": 7650.0,
        "max_accel": 7550.0, "max_magnet": 7650.0,
        "first_positive_above": 7620.0, "expiries": ["2026-09-14"],
        "by_strike": {7550.0: -8.0e8, 7600.0: -2.0e8, 7650.0: 4.0e8},
    }


class Recorder:
    """Captures what actually reached Discord, body and all."""

    def __init__(self):
        self.sent = []

    def send(self, channel, title, body, color=Color.BLUE, fields=None):
        self.sent.append({"channel": channel, "title": title, "body": body,
                          "fields": fields})

    def daily_digest(self, *, body, green_day):
        self.sent.append({"channel": Channel.DAILY, "title": "Daily digest",
                          "body": body, "fields": None})

    def flush(self, timeout=15.0):
        pass

    @property
    def titles(self):
        return [m["title"] for m in self.sent]


@pytest.fixture()
def sink(tmp_path: Path):
    rec = Recorder()
    return rec, AlertSink(rec, StateStore(tmp_path / "s.duckdb"))


@pytest.fixture(autouse=True)
def _midday(monkeypatch):
    """Default every test to inside the alert window unless it says otherwise."""
    monkeypatch.setattr("gexbot.watch.minute_now", lambda: 11 * 60)


# ── 1. the footer is on every structural path ────────────────────────
def test_every_structural_alert_carries_the_position_footer(sink):
    """post_map and regime_flip were both missing it. The footer's whole
    purpose is that no structural message leaves you guessing what the
    shadow book holds, so 'every' has to mean every."""
    rec, s = sink
    st = WatchState(day="2026-09-14")

    post_map(s, profile(-1.0e9), st)

    # regime: establish a baseline, then confirm a flip
    structural_alerts(s, profile(+1.0e9), st)          # baseline, silent
    structural_alerts(s, profile(-1.0e9), st)          # pending
    structural_alerts(s, profile(-1.0e9), st)          # confirmed -> alert

    # flip crossing: spot well below the flip, two polls
    st.last_side_of_flip = "above"
    structural_alerts(s, profile(-1.0e9, spot=7500.0), st)
    structural_alerts(s, profile(-1.0e9, spot=7500.0), st)

    # proximity: spot right at the put wall
    structural_alerts(s, profile(-1.0e9, spot=7551.0), st)

    kinds = {m["title"] for m in rec.sent}
    assert any("gamma map" in t for t in kinds)
    assert any("Regime flip" in t for t in kinds)
    assert any("crossed" in t for t in kinds)
    assert any("approaching" in t for t in kinds)

    for m in rec.sent:
        assert FOOTER_MARK in m["body"], f"no position footer on: {m['title']}"


def test_footer_reports_an_open_position_not_just_flat(sink):
    rec, s = sink
    st = WatchState(day="2026-09-14", open_trade={
        "contracts": 2, "strike": 7550.0, "direction": -1, "expiry": "2026-09-16",
        "entry_premium": 12.5, "opened_at": "2026-09-14T11:00", "entry_spot": 7545.0,
        "level": 7550.0})
    post_map(s, profile(-1.0e9), st)
    assert "2× SPX 7,550P" in rec.sent[0]["body"]


# ── 2. regime dead zone, confirmation, cooldown ──────────────────────
def test_regime_of_refuses_to_call_a_near_zero_book():
    """The live incident: net -12.3M with single strikes at ±9M is aggregate
    noise, and it fired a regime-flip alert."""
    assert regime_of(-12.3e6, peak_abs_net=0.0) == ""
    assert regime_of(REGIME_MIN_ABS_GEX * 0.99, peak_abs_net=0.0) == ""
    assert regime_of(REGIME_MIN_ABS_GEX * 1.01, peak_abs_net=0.0) == "POSITIVE"
    assert regime_of(-1.0e9, peak_abs_net=0.0) == "NEGATIVE"


def test_dead_zone_scales_with_the_session_peak():
    """5% of the session's own largest reading — the same rule replay.py uses,
    so live and replay agree on what a regime is."""
    assert regime_of(400e6, peak_abs_net=10e9) == ""       # under 5% of 10B
    assert regime_of(600e6, peak_abs_net=10e9) == "POSITIVE"


def test_no_alert_while_hovering_in_the_dead_zone(sink):
    rec, s = sink
    st = WatchState(day="2026-09-14")
    # flip=None isolates the regime path: with a flip present this profile
    # legitimately trips the (separate, working) flip-crossing alert.
    for net in (5e6, -6e6, 7e6, -8e6, 3e6, -12.3e6):
        structural_alerts(s, profile(net, flip=None), st)
    assert rec.titles == [], f"dead zone leaked alerts: {rec.titles}"
    assert st.last_regime == "", "no regime was ever claimed"


def test_first_real_reading_is_adopted_without_announcing_a_flip(sink):
    rec, s = sink
    st = WatchState(day="2026-09-14")
    structural_alerts(s, profile(+1.0e9), st)
    assert st.last_regime == "POSITIVE"
    assert not [t for t in rec.titles if "Regime" in t]


def test_regime_flip_needs_two_consecutive_polls(sink):
    rec, s = sink
    st = WatchState(day="2026-09-14")
    structural_alerts(s, profile(+1.0e9), st)          # baseline

    structural_alerts(s, profile(-1.0e9), st)          # first contrary poll
    assert not [t for t in rec.titles if "Regime" in t], "alerted on one poll"
    assert st.pending_regime == "NEGATIVE" and st.pending_regime_count == 1

    structural_alerts(s, profile(-1.0e9), st)          # second: confirmed
    assert [t for t in rec.titles if "Regime" in t] == ["Regime flip → NEGATIVE gamma"]
    assert st.last_regime == "NEGATIVE"


def test_a_single_contrary_poll_is_forgotten(sink):
    """One noisy print between two positive readings must not accumulate."""
    rec, s = sink
    st = WatchState(day="2026-09-14")
    structural_alerts(s, profile(+1.0e9), st)
    structural_alerts(s, profile(-1.0e9), st)          # blip
    structural_alerts(s, profile(+1.0e9), st)          # back
    structural_alerts(s, profile(-1.0e9), st)          # blip again
    assert not [t for t in rec.titles if "Regime" in t]
    assert st.pending_regime_count == 1


def test_regime_cooldown_blocks_a_second_alert_but_tracks_the_state(sink):
    rec, s = sink
    st = WatchState(day="2026-09-14")
    structural_alerts(s, profile(+1.0e9), st)
    for _ in range(2):
        structural_alerts(s, profile(-1.0e9), st)
    assert len([t for t in rec.titles if "Regime" in t]) == 1

    for _ in range(2):                                  # flip straight back
        structural_alerts(s, profile(+1.0e9), st)
    assert len([t for t in rec.titles if "Regime" in t]) == 1, "cooldown leaked"
    assert st.last_regime == "POSITIVE", "state still tracks reality"


# ── body formatting ──────────────────────────────────────────────────
def test_spot_and_flip_moved_into_the_body_and_fields_are_gone(sink):
    rec, s = sink
    st = WatchState(day="2026-09-14")
    structural_alerts(s, profile(+1.0e9), st)
    for _ in range(2):
        structural_alerts(s, profile(-1.0e9, spot=7601.0, flip=7582.5), st)
    msg = [m for m in rec.sent if "Regime" in m["title"]][0]
    assert msg["fields"] is None, "embed fields should be gone"
    assert "Spot 7,601" in msg["body"]
    assert "flip 7,582" in msg["body"]


def test_flip_is_omitted_entirely_when_there_is_none(sink):
    rec, s = sink
    st = WatchState(day="2026-09-14")
    structural_alerts(s, profile(+1.0e9, flip=None), st)
    for _ in range(2):
        structural_alerts(s, profile(-1.0e9, flip=None), st)
    body = [m for m in rec.sent if "Regime" in m["title"]][0]["body"]
    assert "flip" not in body, "should not render a flip placeholder"
    assert "—" not in body.split("Net GEX")[1].split("\n")[0]


# ── 3. the alert window ──────────────────────────────────────────────
@pytest.mark.parametrize("minute,expected", [
    (8 * 60 + 59, False), (ALERT_WINDOW_START, True),
    (11 * 60, True), (ALERT_WINDOW_END, True),
    (16 * 60 + 16, False), (20 * 60 + 17, False),   # the live 20:17 run
])
def test_alert_window_bounds(minute, expected):
    assert in_alert_window(minute) is expected


def test_no_structural_alerts_after_hours_but_the_poll_is_still_recorded(
        tmp_path: Path, monkeypatch):
    """The 20:17 case that started this. History should not have holes just
    because nobody wants a phone notification at 8pm."""
    import duckdb

    from gexbot import watch as w

    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    for v in ("DISCORD_WEBHOOK_TRADES", "DISCORD_WEBHOOK_ALERTS",
              "DISCORD_WEBHOOK_DAILY"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(w.settings, "gex_data_root", tmp_path)
    monkeypatch.setattr(w, "fetch_index_spot", lambda *a, **k: 7657.0)
    monkeypatch.setattr(w, "fetch_chain", lambda *a, **k: ([], 7657.0))
    monkeypatch.setattr(w, "build_profile", lambda *a, **k: profile(-1.0e9))
    monkeypatch.setattr(w, "contract_price", lambda *a, **k: (10.0, 0.5))
    monkeypatch.setattr(w, "minute_now", lambda: 20 * 60 + 17)

    db = tmp_path / "state.duckdb"
    w.run_watch(once=True, shadow=True, state_db=db)

    with duckdb.connect(str(db), read_only=True) as con:
        assert con.execute("SELECT count(*) FROM poll_snapshot").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM alert_log").fetchone()[0] == 0


def test_minute_now_is_exchange_time_not_machine_time(monkeypatch):
    """Every gate in watch.py is defined in ET; reading the wall clock would
    shift all of them on a box in another timezone."""
    monkeypatch.undo()
    from gexbot import watch as w
    from gexbot.clock import ET
    now_et = dt.datetime.now(dt.timezone.utc).astimezone(ET)
    assert w.minute_now() == now_et.hour * 60 + now_et.minute
