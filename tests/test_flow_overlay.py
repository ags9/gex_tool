"""The live flow overlay, end to end: prints -> ledger -> combine -> DuckDB.

This is the path that justifies `livefeed.py` existing. Open interest
publishes once pre-market and reflects yesterday's close, so an OI-only map
is structurally blind to 0DTE — roughly half of SPX volume. Until this was
wired, the feed classified prints into a ledger that nothing ever read.

The feed's WebSocket is stubbed; everything downstream of it — the tick-rule
classifier, the FlowLedger, the gamma lookup, combine(), write_poll — is the
real code.
"""
from pathlib import Path

import duckdb
import pytest

from gexbot.livefeed import FlowLedger, TickRuleClassifier, combine, gamma_lookup
from gexbot.state import StateStore

GAMMA = 0.0030
EXPIRY = "260914"


def chain(strikes=(7550.0, 7600.0, 7650.0)) -> list[dict]:
    """A snapshot in the shape levels.fetch_chain returns."""
    out = []
    for k in strikes:
        for right in ("call", "put"):
            out.append({"details": {"strike_price": k, "contract_type": right,
                                    "expiration_date": "2026-09-14"},
                        "greeks": {"gamma": GAMMA}})
    return out


def oi_profile() -> dict:
    return {"spot": 7600.0, "net": 1.0e8,
            "by_strike": {7550.0: -2.0e8, 7600.0: 0.0, 7650.0: 3.0e8},
            "expiries": ["2026-09-14"], "flip": 7580.0,
            "put_wall": 7550.0, "call_wall": 7650.0,
            "max_accel": 7550.0, "max_magnet": 7650.0,
            "first_positive_above": 7650.0}


# ── gamma lookup ─────────────────────────────────────────────────────
def test_gamma_lookup_keys_match_what_the_feed_parses():
    """OptionsFeed parses OPRA tickers into ('C'|'P', 'YYMMDD'); the snapshot
    speaks ('call', '2026-09-14'). A mismatch here silently zeroes all flow."""
    book = gamma_lookup(chain())
    assert book[(7600.0, "C", EXPIRY)] == GAMMA
    assert book[(7600.0, "P", EXPIRY)] == GAMMA
    assert (7600.0, "call", "2026-09-14") not in book


def test_gamma_lookup_skips_contracts_with_no_greeks():
    assert gamma_lookup([{"details": {"strike_price": 1.0,
                                      "contract_type": "call",
                                      "expiration_date": "2026-09-14"},
                          "greeks": {}}]) == {}


# ── classifier -> ledger ─────────────────────────────────────────────
def test_customer_buying_makes_dealers_shorter_gamma():
    """Sign convention, which is the whole meaning of the overlay: a customer
    BUY leaves dealers shorter gamma at that strike, regardless of call/put."""
    led, cls = FlowLedger(), TickRuleClassifier()
    cls.classify("O:SPXW260914C07600000", 10.0)          # first print: no side
    side = cls.classify("O:SPXW260914C07600000", 10.5)   # uptick -> buy
    assert side == 1
    led.add(7600.0, size=50, side=side, gamma=GAMMA, spot=7600.0)
    assert led.snapshot()[7600.0] < 0
    assert led.contracts_seen == 50 and led.trades_seen == 1


def test_zero_ticks_inherit_the_last_direction():
    cls = TickRuleClassifier()
    t = "O:SPXW260914P07550000"
    cls.classify(t, 5.0)
    assert cls.classify(t, 4.5) == -1
    assert cls.classify(t, 4.5) == -1, "flat print keeps the prior side"


# ── combine ──────────────────────────────────────────────────────────
def test_split_covers_every_merged_strike_and_reconciles():
    merged = combine(oi_profile(), {7550.0: -5.0e7, 7700.0: 1.0e7})
    assert set(merged["oi_by_strike"]) == set(merged["by_strike"])
    assert set(merged["flow_by_strike"]) == set(merged["by_strike"])
    for k, total in merged["by_strike"].items():
        assert merged["oi_by_strike"][k] + merged["flow_by_strike"][k] \
            == pytest.approx(total), f"split does not reconcile at {k}"
    assert merged["net"] == pytest.approx(merged["oi_net"] + merged["flow_net"])


def test_strike_with_no_flow_is_zero_not_missing():
    """Once the overlay runs, flow IS measured everywhere in the window. Zero
    is a measurement; NULL must keep meaning 'overlay was off'."""
    merged = combine(oi_profile(), {7550.0: -5.0e7})
    assert merged["flow_by_strike"][7650.0] == 0.0
    assert 7650.0 in merged["flow_by_strike"]


def test_flow_can_move_the_levels_it_feeds():
    """If the overlay could not change a wall it would be decoration."""
    base = oi_profile()
    moved = combine(base, {7650.0: -8.0e8})          # bury the call wall
    assert base["call_wall"] == 7650.0
    assert moved["net"] < base["net"]
    assert moved["max_accel"] == 7650.0, "flow turned the magnet into an accelerator"


# ── all the way to the database ──────────────────────────────────────
def test_overlay_persists_a_non_null_split(tmp_path: Path):
    """The assertion that matters: flow_gex reaches DuckDB as a number."""
    book = gamma_lookup(chain())
    led, cls = FlowLedger(), TickRuleClassifier()

    # sizes deliberately unequal: a call buy and a put sell of identical size
    # and gamma cancel to a net of exactly zero, which would let a broken
    # net calculation pass.
    prints = [("O:SPXW260914C07650000", 12.0, 100),
              ("O:SPXW260914C07650000", 12.4, 100),
              ("O:SPXW260914P07550000", 8.0, 40),
              ("O:SPXW260914P07550000", 7.6, 40)]
    for ticker, px, size in prints:
        side = cls.classify(ticker, px)
        if not side:
            continue
        strike = float(int(ticker[-8:]) / 1000.0)
        right = ticker[-9]
        led.add(strike, size=size, side=side,
                gamma=book[(strike, right, EXPIRY)], spot=7600.0)

    assert led.trades_seen == 2
    prof = combine(oi_profile(), led.snapshot())
    # call BUY (dealers shorter) outweighs the smaller put SELL
    assert prof["flow_net"] < 0

    store = StateStore(tmp_path / "state.duckdb")
    pid = store.write_poll(prof, {"connected": True,
                                  "trades": led.trades_seen,
                                  "contracts": led.contracts_seen}, 90)
    assert pid is not None

    with duckdb.connect(str(tmp_path / "state.duckdb"), read_only=True) as con:
        oi_net, flow_net, feed_ok, trades = con.execute(
            "SELECT oi_net, flow_net, feed_connected, feed_trades "
            "FROM poll_snapshot").fetchone()
        assert oi_net is not None and flow_net is not None
        assert flow_net != 0.0
        assert feed_ok is True and trades == 2
        assert flow_net == pytest.approx(prof["flow_net"])

        rows = con.execute("SELECT strike, gex, oi_gex, flow_gex "
                           "FROM poll_strike ORDER BY strike").fetchall()
    assert rows, "no strike rows"
    assert all(oi is not None and fl is not None for _, _, oi, fl in rows), \
        "the whole point: no NULLs once the overlay is on"
    for _, gex, oi, fl in rows:
        assert oi + fl == pytest.approx(gex)
    assert any(fl != 0.0 for _, _, _, fl in rows), "no strike carries flow"


def test_overlay_off_still_writes_nulls(tmp_path: Path):
    """The contrast that gives NULL its meaning."""
    store = StateStore(tmp_path / "state.duckdb")
    store.write_poll(oi_profile())
    with duckdb.connect(str(tmp_path / "state.duckdb"), read_only=True) as con:
        assert con.execute("SELECT oi_net, flow_net FROM poll_snapshot"
                           ).fetchone() == (None, None)
        assert all(oi is None and fl is None for oi, fl in con.execute(
            "SELECT oi_gex, flow_gex FROM poll_strike").fetchall())


def test_watch_builds_the_overlay_without_touching_the_network(
        tmp_path: Path, monkeypatch):
    """run_watch with flow on, but OptionsFeed stubbed: proves the wiring in
    watch.py calls combine() and persists the split."""
    from gexbot import watch as w

    led = FlowLedger()
    led.add(7650.0, size=100, side=1, gamma=GAMMA, spot=7600.0)

    class StubFeed:
        connected = True

        def __init__(self, *a, **k):
            self.gamma_fn, self.spot_fn = k.get("gamma_fn"), k.get("spot_fn")

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    for v in ("DISCORD_WEBHOOK_TRADES", "DISCORD_WEBHOOK_ALERTS",
              "DISCORD_WEBHOOK_DAILY"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(w.settings, "gex_data_root", tmp_path)
    monkeypatch.setattr(w, "OptionsFeed", StubFeed)
    monkeypatch.setattr(w, "FlowLedger", lambda: led)
    monkeypatch.setattr(w, "fetch_index_spot", lambda *a, **k: 7600.0)
    monkeypatch.setattr(w, "fetch_chain", lambda *a, **k: (chain(), 7600.0))
    monkeypatch.setattr(w, "build_profile", lambda *a, **k: oi_profile())
    monkeypatch.setattr(w, "minute_now", lambda: 11 * 60)

    db = tmp_path / "state.duckdb"
    w.run_watch(once=True, shadow=False, state_db=db, flow=True)

    with duckdb.connect(str(db), read_only=True) as con:
        oi_net, flow_net, connected = con.execute(
            "SELECT oi_net, flow_net, feed_connected FROM poll_snapshot"
        ).fetchone()
        assert flow_net is not None and flow_net != 0.0
        assert oi_net is not None and connected is True
        assert con.execute("SELECT count(*) FROM poll_strike "
                           "WHERE flow_gex IS NULL").fetchone()[0] == 0
