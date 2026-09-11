"""The --max-dd gate threshold: it must actually judge, and every result must
record which threshold judged it.

PREREGISTRATION section 4 freezes the Stage-2 OOS max drawdown at 20%, while
metrics.GateParams keeps the stricter 12% default deliberately. Both numbers
are therefore in play at once, and a bundle that does not say which one
produced its verdict is not evidence — it is just a number.
"""
import json

import pytest

from gexbot.daysim import TradeRecord
from gexbot.metrics import BacktestReport, GateParams


def _fake_trade(pnl: float) -> TradeRecord:
    return TradeRecord(600, 630, 1, "bounce", 1, 5.0, 5.0 + pnl / 100, "x", pnl, 1.3)


def _oos_with_drawdown(frac: float) -> BacktestReport:
    """OOS book that runs up then gives back `frac` of its peak."""
    peak = 1000.0
    r = BacktestReport(trades=[_fake_trade(80)] * 80 + [_fake_trade(-50)] * 40,
                       daily_pnl=[peak] + [-peak * frac] + [0.0] * 38,
                       start_equity=0.0)
    return r


def test_default_threshold_is_still_twelve_percent():
    """metrics.py's default must not drift — round two relies on 12% being
    the stricter fallback, with 20% passed explicitly."""
    assert GateParams().max_drawdown_frac == 0.12


@pytest.mark.parametrize("dd,at_12,at_20", [
    (0.08, True, True),     # inside both
    (0.16, False, True),    # the band that only 20% admits
    (0.24, False, False),   # outside both
])
def test_threshold_actually_changes_the_verdict(dd, at_12, at_20):
    is_ = BacktestReport(trades=[_fake_trade(80)] * 240 + [_fake_trade(-50)] * 120,
                         daily_pnl=[40.0] * 120)
    oos = _oos_with_drawdown(dd)
    assert is_.gates(oos, GateParams())["g3_max_drawdown"][0] is at_12
    assert is_.gates(oos, GateParams(max_drawdown_frac=0.20))["g3_max_drawdown"][0] is at_20


def test_gate_detail_names_the_threshold_it_used():
    """The per-gate detail string is what lands in summary.md and the console
    table; reading it must tell you which threshold judged the run."""
    is_ = BacktestReport(trades=[_fake_trade(80)] * 10, daily_pnl=[40.0] * 10)
    oos = _oos_with_drawdown(0.16)
    assert "20.0%" in is_.gates(oos, GateParams(max_drawdown_frac=0.20))["g3_max_drawdown"][1]
    assert "12.0%" in is_.gates(oos, GateParams())["g3_max_drawdown"][1]


def test_bundle_records_thresholds_and_explore_can_read_both_shapes():
    """gates.json nests {gate_params, gates}; explore.py must also survive the
    flat pre-flag bundles still sitting in data/results."""
    gp = GateParams(max_drawdown_frac=0.20)
    is_ = BacktestReport(trades=[_fake_trade(80)] * 10, daily_pnl=[40.0] * 10)
    gates = is_.gates(_oos_with_drawdown(0.16), gp)

    from dataclasses import asdict
    new_shape = json.loads(json.dumps({
        "gate_params": asdict(gp),
        "gates": {k: {"passed": v[0], "detail": v[1]} for k, v in gates.items()},
    }))
    assert new_shape["gate_params"]["max_drawdown_frac"] == 0.20

    # the exact two lines explore.py uses
    for blob, expect_params in ((new_shape, True),
                                (new_shape["gates"], False)):
        parsed_gates = blob.get("gates", blob)
        parsed_params = blob.get("gate_params", {})
        assert "g3_max_drawdown" in parsed_gates
        assert all("passed" in g for g in parsed_gates.values())
        assert bool(parsed_params) is expect_params
