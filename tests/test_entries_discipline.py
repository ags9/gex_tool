"""Tests for entries + discipline: the range-day dance, the breakout kill,
the revenge lockout, and — most importantly — the C.11 veto that blocks
the operator's historical averaging-down disaster setup.
"""
from gexbot.discipline import DisciplineState
from gexbot.entries import EntryEngine, MarketState


def base_state(**kw) -> MarketState:
    d = dict(
        minute=11 * 60, spot=7600.0, session_high=7620.0, regime="P",
        put_wall=7550.0, call_wall=7660.0, g_max=7595.0,
        bar_close_above_level=False, bar_break_below_level=False,
        option_spread=0.10, news_blocked=False,
        bars_since_new_high=0, lower_high_close_below_prior_low=False,
    )
    d.update(kw)
    return MarketState(**d)


def test_bounce_entry_fires():
    eng = EntryEngine()
    m = base_state(spot=7597.0, bar_close_above_level=True)  # at G_MAX 7595, confirmed
    sig = eng.evaluate(m)
    assert sig and sig.direction == +1 and sig.kind == "bounce" and sig.level == 7595.0


def test_no_entry_without_confirmation_bar():
    eng = EntryEngine()
    assert eng.evaluate(base_state(spot=7597.0)) is None  # at level but no bounce bar


def test_c11_veto_blocks_macro_sponsored_dip_buy():
    """The averaging-down disaster setup: big selloff WITH macro sponsorship
    (JPY bid, yields moving, sev2 tape). Bounce bar prints at the put wall —
    and the veto blocks the call. This is the crash-day filter."""
    eng = EntryEngine()
    m = base_state(
        spot=7552.0, session_high=7660.0,            # down ~1.6%
        bar_close_above_level=True, g_max=7550.5,     # 'bounce' printing at wall
        usdjpy_flat_or_up_60m=False, yield_stable=False, no_recent_sev2_90m=False,
    )
    assert eng.reversal_score(m) == 20                # only wall confluence
    assert eng.evaluate(m) is None
    assert eng.blocked_log and eng.blocked_log[-1][1].blocked_by == "C.11 macro veto"


def test_c11_enables_reversal_entry_when_macro_benign():
    """Same selloff, benign macro: score 100 -> entry permitted as 'reversal',
    at standard size (sizing lives elsewhere and never sees the score)."""
    eng = EntryEngine()
    m = base_state(spot=7552.0, session_high=7660.0,
                   bar_close_above_level=True, g_max=7550.5)
    assert eng.reversal_score(m) == 100
    sig = eng.evaluate(m)
    assert sig and sig.kind == "reversal" and sig.direction == +1


def test_flip_trigger_range_day():
    eng = EntryEngine()
    m = base_state(spot=7659.0, bars_since_new_high=3)   # at call wall, stalled
    sig = eng.flip_trigger(m, position_direction=+1,
                           position_profitable=True, flip_mode_alive=True)
    assert sig and sig.direction == -1 and sig.kind == "flip"


def test_flip_never_rescues_a_loser():
    eng = EntryEngine()
    m = base_state(spot=7659.0, bars_since_new_high=3)
    assert eng.flip_trigger(m, +1, position_profitable=False, flip_mode_alive=True) is None


def test_flip_dies_with_regime():
    eng = EntryEngine()
    m = base_state(spot=7659.0, bars_since_new_high=3, regime="N")
    assert eng.flip_trigger(m, +1, True, True) is None


def test_discipline_stopout_lockout_and_daily_stop():
    d = DisciplineState()
    ok, _ = d.may_enter(minute=600, direction=1, is_reentry_after_trail=False, is_range_day=False)
    assert ok
    d.on_entry(direction=1, is_reentry_after_trail=False)
    d.on_exit(pnl_dollars=-150, was_stopout=True, was_trail=False, was_flip=False, minute=620)
    ok, why = d.may_enter(minute=635, direction=1, is_reentry_after_trail=False, is_range_day=False)
    assert not ok and "lockout" in why                    # 30-min cooloff
    ok, _ = d.may_enter(minute=651, direction=1, is_reentry_after_trail=False, is_range_day=False)
    assert ok
    d.on_entry(direction=1, is_reentry_after_trail=False)
    d.on_exit(pnl_dollars=-140, was_stopout=True, was_trail=False, was_flip=False, minute=700)
    ok, why = d.may_enter(minute=800, direction=1, is_reentry_after_trail=False, is_range_day=False)
    assert not ok and "done for the day" in why           # 2 losers = done


def test_discipline_trail_reentry_rules():
    d = DisciplineState()
    d.on_entry(direction=1, is_reentry_after_trail=False)
    d.on_exit(pnl_dollars=+220, was_stopout=False, was_trail=True, was_flip=False, minute=660)
    ok, why = d.may_enter(minute=662, direction=1, is_reentry_after_trail=True, is_range_day=False)
    assert not ok and "one full bar" in why               # C.5b wait
    ok, _ = d.may_enter(minute=666, direction=1, is_reentry_after_trail=True, is_range_day=False)
    assert ok                                             # no fear, no lockout — profit exit


def test_consecutive_losing_flips_kill_flip_mode():
    d = DisciplineState()
    for minute in (700, 730):
        d.on_entry(direction=1, is_reentry_after_trail=False)
        d.on_exit(pnl_dollars=-60, was_stopout=False, was_trail=False, was_flip=True, minute=minute)
    assert d.flip_mode_dead
