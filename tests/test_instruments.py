"""Instrument scaling and the S&P-complex merge (spec §10.2, §10.3)."""
import pytest

from gexbot.instruments import (COMPLEX, INSTRUMENTS, SPX, SPY, instrument,
                                merge_complex, observed_ratio, root_owner,
                                to_spx_scale)


def spy_profile() -> dict:
    return {"spot": 764.0, "net": 2.0e6,
            "by_strike": {760.0: -1.0e6, 765.0: 3.0e6},
            "oi_by_strike": {760.0: -1.0e6, 765.0: 3.0e6},
            "flow_by_strike": {760.0: 0.0, 765.0: 0.0},
            "volume_by_strike": {760.0: 4000.0, 765.0: 9000.0},
            "oi_net": 2.0e6, "flow_net": 0.0,
            "put_wall": 760.0, "call_wall": 765.0, "flip": 763.0,
            "max_accel": 760.0, "max_magnet": 765.0,
            "first_positive_above": 765.0, "expiries": ["2026-09-14"]}


def spx_profile() -> dict:
    return {"spot": 7650.0, "net": -1.2e7,
            "by_strike": {7600.0: -8.0e6, 7650.0: -4.0e6},
            "oi_by_strike": {7600.0: -8.0e6, 7650.0: -4.0e6},
            "flow_by_strike": {7600.0: 0.0, 7650.0: 0.0},
            "volume_by_strike": {7600.0: 1000.0},
            "oi_net": -1.2e7, "flow_net": 0.0,
            "put_wall": 7600.0, "call_wall": 7650.0, "flip": 7640.0,
            "expiries": ["2026-09-14"]}


def test_root_ownership_keeps_the_books_apart():
    """SPX and SPXW share a book; SPY has its own. A shared book would add a
    SPY 765 print to an SPX 765 strike and corrupt both maps."""
    assert root_owner("SPX") == root_owner("SPXW") == SPX
    assert root_owner("SPY") == SPY
    assert root_owner("QQQ") is None


def test_spy_scales_strikes_up_and_gamma_down():
    """The two conversions go in OPPOSITE directions. Getting the gamma one
    backwards would inflate SPY's contribution a hundredfold and silently
    dominate the merged map."""
    out = to_spx_scale(spy_profile(), INSTRUMENTS[SPY], 10.0)
    assert out["by_strike"] == {7600.0: -1.0e5, 7650.0: 3.0e5}
    assert out["spot"] == 7640.0
    assert out["put_wall"] == 7600.0 and out["call_wall"] == 7650.0
    assert out["net"] == pytest.approx(2.0e5)


def test_ratio_is_measured_not_assumed():
    """The amendment: SPY x10 is not SPX. The ETF carries a basis to the
    index, and a constant places every SPY strike off its true SPX level."""
    assert observed_ratio(7650.0, 764.0, 10.0) == pytest.approx(7650.0 / 764.0)
    assert observed_ratio(7650.0, 764.0, 10.0) != 10.0
    # missing either spot falls back rather than dividing by nothing
    assert observed_ratio(None, 764.0, 10.0) == 10.0
    assert observed_ratio(7650.0, 0.0, 10.0) == 10.0


def test_merge_uses_the_observed_ratio_and_reports_it():
    m = merge_complex({SPX: spx_profile(), SPY: spy_profile()})
    r = 7650.0 / 764.0
    assert m["spy_ratio"] == pytest.approx(r, rel=1e-6)
    assert m["scale_ratios"][SPY] == pytest.approx(r, rel=1e-6)
    # The SPY 765 strike lands on its measured equivalent (~7,660), NOT on
    # the naive 7,650 — which is the whole point of the amendment. 7,650 is
    # still present, but as SPX's own strike carrying SPX's own gamma.
    spy_765 = 765.0 * r
    assert spy_765 == pytest.approx(7660.01, abs=0.05)
    assert any(abs(k - spy_765) < 1e-6 for k in m["by_strike"])
    assert m["by_strike"][7650.0] == pytest.approx(spx_profile()["by_strike"][7650.0]), \
        "SPX 7,650 keeps its own gamma; no SPY contribution lands on it"


def test_volume_is_a_count_and_is_not_divided():
    out = to_spx_scale(spy_profile(), INSTRUMENTS[SPY])
    assert out["volume_by_strike"] == {7600.0: 4000.0, 7650.0: 9000.0}


def test_spx_passes_through_untouched():
    p = spx_profile()
    assert to_spx_scale(p, INSTRUMENTS[SPX])["by_strike"] == p["by_strike"]


def test_merge_is_exactly_additive_on_the_spx_axis():
    m = merge_complex({SPX: spx_profile(), SPY: spy_profile()})
    r = 7650.0 / 764.0
    assert m["net"] == pytest.approx(
        spx_profile()["net"] + spy_profile()["net"] / r)
    assert m["net"] == pytest.approx(sum(m["by_strike"].values()))
    assert m["components"] == [SPX, SPY]


def test_merge_recomputes_levels_rather_than_inheriting_them():
    """A wall is wherever the COMBINED gamma peaks. Inheriting SPX's walls
    would produce a chart that looks combined and is not."""
    spy = spy_profile()
    spy["by_strike"] = {770.0: 9.0e7}          # a wall SPX does not have
    spy["oi_by_strike"] = dict(spy["by_strike"])
    m = merge_complex({SPX: spx_profile(), SPY: spy})
    expected = 770.0 * (7650.0 / 764.0)
    assert m["call_wall"] == pytest.approx(expected, rel=1e-6), \
        "the merged wall, not SPX's"
    assert m["max_magnet"] == pytest.approx(expected, rel=1e-6)


def test_merge_of_one_instrument_is_that_instrument():
    m = merge_complex({SPX: spx_profile()})
    assert m["by_strike"] == spx_profile()["by_strike"]


def test_unknown_instrument_is_refused_not_guessed():
    with pytest.raises(SystemExit):
        instrument("QQQ")
    assert COMPLEX not in INSTRUMENTS, "COMPLEX is derived, never fetched"
