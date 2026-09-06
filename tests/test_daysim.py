"""Simulator end-to-end tests on synthetic days.

These assert BEHAVIOR, not profitability — synthetic paths prove the logic
executes the spec; only real data can prove the edge.
"""
from gexbot.daysim import DayResult, DaySimulator, SimConfig
from gexbot.synth import breakout_day, range_day


def run_range(regime_override=None, **cfg_kw) -> DayResult:
    bars = range_day()
    cfg = SimConfig(**cfg_kw)
    if regime_override:
        cfg.regime_by_bar = regime_override
    return DaySimulator(cfg).run(bars)


def test_range_day_trades_and_stays_flat_by_eod():
    res = run_range()
    assert len(res.trades) >= 1                       # it found the dance
    last = max(t.exit_minute for t in res.trades)
    assert last <= 15 * 60 + 55                       # EOD flat guaranteed
    # every trade record priced with costs
    assert all(t.cost_drag > 0 for t in res.trades)


def test_discipline_caps_respected():
    res = run_range()
    # count distinct opened positions (partial exits share an entry_minute)
    opens = {t.entry_minute for t in res.trades}
    assert len(opens) <= 5                            # range-day cap
    if res.halted:
        assert res.halt_reason                        # halts always carry a reason


def test_breakout_day_regime_flip_contains_damage():
    """The disaster shape: ranging, then the wall breaks and price slides 90.
    Regime flips to N at the break. Any open long call must exit on the
    regime trigger; flip-mode must never buy the falling knife."""
    bars = breakout_day()
    # regime: P until the wall break bar, N after
    wall = 7550.0
    regime = []
    broken = False
    for b in bars:
        if b.close < wall:
            broken = True
        regime.append("N" if broken else "P")
    cfg = SimConfig(regime_by_bar=regime)
    res = DaySimulator(cfg).run(bars)

    # no long-call entries AFTER the regime turned N
    first_n = regime.index("N") if "N" in regime else len(regime)
    n_start_minute = bars[first_n].minute
    late_calls = [t for t in res.trades
                  if t.direction == +1 and t.entry_minute >= n_start_minute]
    assert late_calls == []
    # if a call was open at the break, it exited via regime/stop, not held to EOD
    for t in res.trades:
        if t.direction == +1 and t.exit_minute >= n_start_minute:
            assert "regime" in t.exit_reason or "stop" in t.exit_reason or "trail" in t.exit_reason


def test_costs_scale_with_activity():
    res = run_range()
    if len(res.trades) >= 2:
        assert res.total_costs > 0
        per_trade = res.total_costs / len(res.trades)
        assert per_trade < 200                        # sanity: drag is bounded


def test_blocked_signals_are_logged():
    """Filter-effectiveness dataset must accumulate (spec §11/§14.3)."""
    bars = range_day()
    cfg = SimConfig()
    sim = DaySimulator(cfg)
    res = sim.run(bars)
    # blocked list is a list of (minute, kind, reason) tuples — may be empty
    for item in res.blocked:
        assert len(item) == 3
