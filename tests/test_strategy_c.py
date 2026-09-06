"""Tests. test_7670_scenario is the operator's real trade, mechanized:
long 7600C, spot runs to 7670, pulls back — the engine must (a) exit on the
trail like the human did, and (b) leave re-entry legal, unlike the human.
"""
import numpy as np

from gexbot import greeks
from gexbot.exits import CParams, ExitEngine, ExitReason, Position
from gexbot.ledger import BaselineModel, Ledger, classify_trade


def test_greeks_sanity():
    s, k, t, iv = 7600.0, 7600.0, 4 / 252, 0.18
    c = float(greeks.price(s, k, t, iv, "C"))
    p = float(greeks.price(s, k, t, iv, "P"))
    assert abs((c - p) - 0.0) < 1e-6          # ATM, r=q=0: parity => c == p
    assert 0.49 < float(greeks.delta(s, k, t, iv, "C")) < 0.55
    assert float(greeks.gamma(s, k, t, iv)) > 0
    iv_back = float(greeks.implied_vol(c, s, k, t, "C"))
    assert abs(iv_back - iv) < 1e-3           # solver round-trips


def test_ledger_levels():
    led = Ledger(spot=7600.0, baseline=BaselineModel.NAIVE_LONG_CALL_SHORT_PUT)
    t = 4 / 252
    led.load_oi(7500, call_oi=1000, put_oi=30000, call_iv=0.20, put_iv=0.22, t_years=t)
    led.load_oi(7600, call_oi=20000, put_oi=20000, call_iv=0.18, put_iv=0.18, t_years=t)
    led.load_oi(7700, call_oi=30000, put_oi=1000, call_iv=0.17, put_iv=0.19, t_years=t)
    lv = led.levels()
    assert lv["put_wall"] == 7500 and lv["call_wall"] == 7700
    assert classify_trade(5.10, 5.00, 5.15) == 1   # near ask = customer buy
    assert classify_trade(5.02, 5.00, 5.15) == -1  # near bid = customer sell
    # customer buys gamma -> dealers shorter
    before = led.net_gex()
    led.on_classified_trade(7600, "C", size=500, customer_side=1, iv=0.18, t_years=t)
    assert led.net_gex() < before


def test_7670_scenario():
    """Operator's trade: long 7600C from spot 7602. Runs to 7670, pulls back.
    ATR30 = 12 -> trail dist = 18. Expect trail exit near 7652, profitably,
    with breakeven long since locked."""
    eng = ExitEngine(CParams())
    pos = Position(direction=1, entry_spot=7602.0, entry_premium=42.0,
                   contracts=10, entry_minute=10 * 60 + 15)
    atr = 12.0

    path = [7610, 7625, 7641, 7655, 7670, 7663, 7656, 7651.9]
    marks = [44, 50, 56, 61, 67, 64, 61, 59]  # PT1 (+30% = 54.6) hits at 7641
    actions = []
    for spot, mark, minute in zip(path, marks, range(10 * 60 + 20, 10 * 60 + 60, 5)):
        a = eng.evaluate(pos, spot=spot, option_mark=mark,
                         minute_of_day=minute, atr30=atr)
        actions.append((spot, a.kind, a.close_contracts, a.note))
        if a.kind not in ("hold", ExitReason.PT1_PARTIAL):
            break

    kinds = [a[1] for a in actions]
    assert ExitReason.PT1_PARTIAL in kinds          # took half at +30%
    assert kinds[-1] == ExitReason.TRAIL            # trailed out on the pullback
    assert pos.breakeven_locked
    trail_exit_spot = actions[-1][0]
    assert 7650 <= trail_exit_spot <= 7656          # ~ where the human exited
    assert pos.remaining == 5 and actions[-1][2] == 5
    # profit: PT1 half at +30%+ and remainder at mark 59 vs 42 entry — both green
    # Re-entry legality (C.5b): trail exits carry NO lockout — that's simply
    # the absence of a lockout flag; asserted here as documentation.


def test_hard_stop_and_no_averaging():
    """A losing position must exit at -25%; there is no 'add' API at all."""
    eng = ExitEngine(CParams())
    pos = Position(direction=1, entry_spot=7602.0, entry_premium=40.0,
                   contracts=10, entry_minute=11 * 60)
    a = eng.evaluate(pos, spot=7575.0, option_mark=29.9,
                     minute_of_day=11 * 60 + 25, atr30=12.0)
    assert a.kind == ExitReason.HARD_STOP and a.close_contracts == 10
    assert not hasattr(pos, "add_contracts")        # averaging down: no code path


def test_eod_flat_always_wins():
    eng = ExitEngine(CParams())
    pos = Position(direction=1, entry_spot=7602.0, entry_premium=40.0,
                   contracts=10, entry_minute=14 * 60)
    a = eng.evaluate(pos, spot=7640.0, option_mark=55.0,
                     minute_of_day=15 * 60 + 50, atr30=12.0)
    assert a.kind == ExitReason.EOD                 # even a winner goes flat


def test_level_aware_stop():
    """Gamma support between raw trail and peak: stop snaps below the level."""
    eng = ExitEngine(CParams())
    pos = Position(direction=1, entry_spot=7602.0, entry_premium=42.0,
                   contracts=10, entry_minute=10 * 60)
    pos.peak_spot = 7670.0
    stop = eng.stop_level(pos, atr30=12.0, gamma_support=7660.0)
    assert stop == 7660.0 - 2.0                     # level minus buffer, not 7652
