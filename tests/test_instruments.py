"""Instrument scaling and the S&P-complex merge (spec §10.2, §10.3)."""
import pytest

from gexbot.instruments import (COMPLEX, INSTRUMENTS, SPX, SPY, instrument,
                                merge_complex, root_owner, to_spx_scale)


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
    out = to_spx_scale(spy_profile(), INSTRUMENTS[SPY])
    assert out["by_strike"] == {7600.0: -1.0e5, 7650.0: 3.0e5}
    assert out["spot"] == 7640.0
    assert out["put_wall"] == 7600.0 and out["call_wall"] == 7650.0
    assert out["net"] == pytest.approx(2.0e5)


def test_volume_is_a_count_and_is_not_divided():
    out = to_spx_scale(spy_profile(), INSTRUMENTS[SPY])
    assert out["volume_by_strike"] == {7600.0: 4000.0, 7650.0: 9000.0}


def test_spx_passes_through_untouched():
    p = spx_profile()
    assert to_spx_scale(p, INSTRUMENTS[SPX])["by_strike"] == p["by_strike"]


def test_merge_is_exactly_additive_on_the_spx_axis():
    m = merge_complex({SPX: spx_profile(), SPY: spy_profile()})
    assert m["by_strike"][7600.0] == pytest.approx(-8.0e6 + -1.0e5)
    assert m["by_strike"][7650.0] == pytest.approx(-4.0e6 + 3.0e5)
    assert m["net"] == pytest.approx(spx_profile()["net"] + spy_profile()["net"] / 10)
    assert m["net"] == pytest.approx(sum(m["by_strike"].values()))
    assert m["components"] == [SPX, SPY]


def test_merge_recomputes_levels_rather_than_inheriting_them():
    """A wall is wherever the COMBINED gamma peaks. Inheriting SPX's walls
    would produce a chart that looks combined and is not."""
    spy = spy_profile()
    spy["by_strike"] = {770.0: 9.0e7}          # a wall SPX does not have
    spy["oi_by_strike"] = dict(spy["by_strike"])
    m = merge_complex({SPX: spx_profile(), SPY: spy})
    assert m["call_wall"] == 7700.0, "the merged wall, not SPX's"
    assert m["max_magnet"] == 7700.0


def test_merge_of_one_instrument_is_that_instrument():
    m = merge_complex({SPX: spx_profile()})
    assert m["by_strike"] == spx_profile()["by_strike"]


def test_unknown_instrument_is_refused_not_guessed():
    with pytest.raises(SystemExit):
        instrument("QQQ")
    assert COMPLEX not in INSTRUMENTS, "COMPLEX is derived, never fetched"
