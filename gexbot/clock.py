"""Exchange clock — the single source of truth for US/Eastern conversion.

Every timestamp in this project arrives as UTC nanoseconds (OPRA
`sip_timestamp`, index flat files, REST aggregates) while every trading rule
is written in Eastern wall-clock minutes-of-day. Converting between the two
with a fixed offset is wrong for roughly a third of the year, and the three
modules that did it had each picked a different constant:

    replay.py      -5   (EST)   bars, quote books, flow bucketing
    parity.py      -4   (EDT)   reconstructed spot
    marks_rest.py  -4   (EDT)   REST NBBO marks

So the same instant landed in different 5-minute bars depending on which
module read it, and every date on the wrong side of a DST boundary was off
by a full hour — an hour that lands squarely inside the 09:45-14:30 entry
window. zoneinfo handles the DST calendar; keeping the scalar and vectorized
forms in one module is what stops the constants drifting apart again.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import polars as pl

ET = ZoneInfo("America/New_York")
ET_NAME = "America/New_York"


def minute_of_day_et(ts_ns: int) -> int:
    """UTC nanoseconds -> Eastern minute-of-day. 09:30 ET -> 570, any season."""
    t = dt.datetime.fromtimestamp(ts_ns / 1e9, tz=dt.timezone.utc).astimezone(ET)
    return t.hour * 60 + t.minute


def minute_of_day_expr(col: str) -> pl.Expr:
    """Vectorized `minute_of_day_et` for an Int64 column of UTC nanoseconds.

    The casts are load-bearing: `dt.hour()` yields Int8, so `hour * 60`
    silently overflows (9*60 wraps to 28) and every bucket is wrong without
    them. Caller applies its own `.alias()`.
    """
    et = (pl.from_epoch(pl.col(col), time_unit="ns")
            .dt.replace_time_zone("UTC")
            .dt.convert_time_zone(ET_NAME))
    return et.dt.hour().cast(pl.Int64) * 60 + et.dt.minute().cast(pl.Int64)


def et_minute_to_utc_ns(day: dt.date, minute: int) -> int:
    """Eastern wall-clock minute-of-day on `day` -> UTC nanoseconds.

    Inverse of `minute_of_day_et` for any minute in a normal session; the
    ambiguous/nonexistent hours of a DST transition fall outside 09:30-16:00
    on every US transition date, so session timestamps round-trip exactly.
    """
    local = dt.datetime.combine(day, dt.time(minute // 60, minute % 60), tzinfo=ET)
    return int(local.astimezone(dt.timezone.utc).timestamp() * 1e9)
