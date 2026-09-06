"""Synthetic 5-min bar generator — range days and breakout days with known
structure, so the simulator's behavior can be asserted before real data
arrives. NOT a market model; a test fixture with controllable shape.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

SESSION_START = 9 * 60 + 30
SESSION_END = 16 * 60
BAR_MIN = 5


@dataclass
class Bar:
    minute: int          # minutes-of-day at bar CLOSE
    open: float
    high: float
    low: float
    close: float


def _bars_from_path(closes: np.ndarray, wiggle: float, rng) -> list[Bar]:
    bars = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        hi = max(o, c) + abs(rng.normal(0, wiggle))
        lo = min(o, c) - abs(rng.normal(0, wiggle))
        bars.append(Bar(SESSION_START + (i + 1) * BAR_MIN, o, hi, lo, c))
        prev = c
    return bars


def range_day(*, seed: int = 7, low: float = 7550.0, high: float = 7660.0,
              start: float = 7580.0, cycles: float = 2.25,
              noise: float = 3.0) -> list[Bar]:
    """Oscillates between the walls (positive-GEX pinning shape)."""
    rng = np.random.default_rng(seed)
    n = (SESSION_END - SESSION_START) // BAR_MIN
    t = np.linspace(0, 1, n)
    mid, amp = (high + low) / 2, (high - low) / 2 * 0.92
    phase = np.arcsin(np.clip((start - mid) / amp, -1, 1))
    closes = mid + amp * np.sin(2 * np.pi * cycles * t + phase)
    closes += rng.normal(0, noise, n).cumsum() * 0.15
    closes = np.clip(closes, low + 2, high - 2)
    return _bars_from_path(closes, wiggle=2.0, rng=rng)


def breakout_day(*, seed: int = 11, start: float = 7600.0,
                 wall: float = 7550.0, break_bar: int = 30,
                 slide: float = 90.0, noise: float = 3.0) -> list[Bar]:
    """Ranges above the put wall, then breaks it and trends down hard —
    the 'range day that becomes a breakout day' disaster shape."""
    rng = np.random.default_rng(seed)
    n = (SESSION_END - SESSION_START) // BAR_MIN
    closes = np.empty(n)
    closes[:break_bar] = start + rng.normal(0, noise, break_bar).cumsum() * 0.4
    closes[:break_bar] = np.clip(closes[:break_bar], wall + 5, start + 25)
    down = np.linspace(0, slide, n - break_bar)
    closes[break_bar:] = closes[break_bar - 1] - down + rng.normal(0, noise, n - break_bar)
    return _bars_from_path(closes, wiggle=2.5, rng=rng)


def atr30(bars: list[Bar], i: int) -> float:
    """ATR over trailing 6 five-min bars (30 minutes), min 2 pts."""
    lo = max(0, i - 5)
    trs = [b.high - b.low for b in bars[lo:i + 1]]
    return max(float(np.mean(trs)), 2.0)
