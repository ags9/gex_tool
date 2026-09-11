"""Timezone bucketing across the DST boundary, and the shuffled_levels
derangement.

Both guard the same class of failure: a bug that makes results look fine.
A fixed UTC offset silently shifts a third of the year's bars by an hour —
an hour that lands inside the 09:45-14:30 entry window — and a self-donating
shuffle silently turns the sharpest null arm into the real strategy.
"""
import datetime as dt
import random

import polars as pl
import pytest

from gexbot.clock import et_minute_to_utc_ns, minute_of_day_et, minute_of_day_expr
from gexbot.control import derangement

# 09:30 ET open, both sides of the DST boundary.
SUMMER = dt.date(2025, 6, 10)   # EDT, UTC-4
WINTER = dt.date(2025, 1, 15)   # EST, UTC-5
OPEN_MIN = 9 * 60 + 30          # 570
CLOSE_MIN = 15 * 60 + 55        # 955


@pytest.mark.parametrize("day,utc_hour", [(SUMMER, 13), (WINTER, 14)])
def test_open_buckets_to_570_in_both_seasons(day: dt.date, utc_hour: int):
    """The same wall-clock open is a DIFFERENT UTC hour in each season;
    both must bucket to minute 570. A hardcoded offset gets one of these
    wrong by exactly 60 minutes."""
    ts = int(dt.datetime(day.year, day.month, day.day, utc_hour, 30,
                         tzinfo=dt.timezone.utc).timestamp() * 1e9)
    assert minute_of_day_et(ts) == OPEN_MIN


@pytest.mark.parametrize("day", [SUMMER, WINTER])
@pytest.mark.parametrize("minute", [OPEN_MIN, 11 * 60, CLOSE_MIN])
def test_round_trip_is_exact(day: dt.date, minute: int):
    assert minute_of_day_et(et_minute_to_utc_ns(day, minute)) == minute


@pytest.mark.parametrize("day", [SUMMER, WINTER])
def test_vectorized_matches_scalar(day: dt.date):
    """The polars path and the python path must agree — replay.py uses the
    expr for bars and the scalar for quote books on the SAME session."""
    minutes = list(range(OPEN_MIN, 16 * 60, 7))
    ts = [et_minute_to_utc_ns(day, m) for m in minutes]
    got = (pl.DataFrame({"t": ts})
             .with_columns(minute_of_day_expr("t").alias("mod"))["mod"].to_list())
    assert got == minutes == [minute_of_day_et(t) for t in ts]


def test_vectorized_does_not_overflow_int8():
    """dt.hour() is Int8: `hour * 60` wraps (9*60 -> 28) without a cast.
    This is the regression that made every bucket wrong but plausible."""
    ts = [et_minute_to_utc_ns(SUMMER, m) for m in (OPEN_MIN, 12 * 60, CLOSE_MIN)]
    got = (pl.DataFrame({"t": ts})
             .with_columns(minute_of_day_expr("t").alias("mod"))["mod"])
    assert got.to_list() == [OPEN_MIN, 12 * 60, CLOSE_MIN]
    assert got.dtype == pl.Int64


def test_summer_and_winter_differ_by_an_hour_in_utc():
    """Guards the fixture logic itself: if these were equal, the test above
    would pass under a hardcoded offset too."""
    assert (et_minute_to_utc_ns(WINTER, OPEN_MIN) % 86_400_000_000_000
            - et_minute_to_utc_ns(SUMMER, OPEN_MIN) % 86_400_000_000_000
            == 3_600_000_000_000)


# ── shuffled_levels derangement ──────────────────────────────────────
@pytest.mark.parametrize("n", [2, 3, 5, 20, 97])
def test_derangement_has_no_fixed_point(n: int):
    for seed in range(50):
        idx = derangement(n, random.Random(seed))
        assert sorted(idx) == list(range(n)), "must stay a permutation"
        assert all(idx[i] != i for i in range(n)), "no day may donate to itself"


def test_derangement_degenerate_sizes():
    """n < 2 has no derangement; return identity rather than loop forever.
    run_control warns the operator that the arm is meaningless there."""
    assert derangement(0, random.Random(0)) == []
    assert derangement(1, random.Random(0)) == [0]


def test_derangement_is_not_constant():
    """A fixed-point-free permutation that never varies would be a different
    silent bias — the cyclic fallback must be the exception, not the rule."""
    seen = {tuple(derangement(6, random.Random(s))) for s in range(30)}
    assert len(seen) > 1
