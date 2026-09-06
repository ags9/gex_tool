"""OPRA/OCC option symbol parsing.

Flat-file tickers look like:  O:SPXW251219C06000000
    O:            options prefix
    SPXW          underlying root (SPX monthlys=SPX, weeklys/0DTE=SPXW)
    251219        expiry YYMMDD
    C / P         right
    06000000      strike * 1000, zero-padded to 8

We parse with slicing from the right (root length varies, the rest is fixed).
"""
from __future__ import annotations

import datetime as dt
from typing import NamedTuple


class OptionSymbol(NamedTuple):
    root: str          # e.g. "SPXW"
    expiry: dt.date
    right: str         # "C" or "P"
    strike: float


def parse_option_ticker(ticker: str) -> OptionSymbol | None:
    """Parse an OPRA ticker. Returns None if malformed (log, don't crash)."""
    t = ticker[2:] if ticker.startswith("O:") else ticker
    # Fixed tail: 6 (date) + 1 (right) + 8 (strike) = 15 chars
    if len(t) < 16:
        return None
    root, tail = t[:-15], t[-15:]
    date_s, right, strike_s = tail[:6], tail[6], tail[7:]
    if right not in ("C", "P") or not strike_s.isdigit() or not date_s.isdigit():
        return None
    try:
        expiry = dt.datetime.strptime(date_s, "%y%m%d").date()
    except ValueError:
        return None
    return OptionSymbol(root=root, expiry=expiry, right=right, strike=int(strike_s) / 1000.0)


def root_of(ticker: str) -> str:
    """Fast root extraction for filtering (no full parse)."""
    t = ticker[2:] if ticker.startswith("O:") else ticker
    return t[:-15] if len(t) >= 16 else ""
