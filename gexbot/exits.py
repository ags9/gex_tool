"""Position manager implementing Strategy C exits (spec C.5 / C.5a).

Pure logic, no I/O: feed it bars/ticks, it emits actions. The same class
runs in the backtest simulator and (later) the live engine — one exit
brain, two harnesses, which is what makes paper/live signal-parity checks
meaningful.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ExitReason(str, Enum):
    TRAIL = "trail_stop"
    HARD_STOP = "hard_stop"
    PT1_PARTIAL = "pt1_partial"
    LEVEL_INVALID = "level_invalidation"
    TIME_STOP = "time_stop"
    EOD = "eod_flat"
    REGIME = "regime_or_news"


@dataclass
class CParams:
    """⚙ parameters from spec C.5/C.5a — the backtest sweeps these."""
    pt1_pct: float = 0.30            # +30% premium -> partial
    pt1_fraction: float = 0.5        # sell half
    hard_stop_pct: float = 0.25      # -25% premium -> out
    trail_atr_mult: float = 1.5
    trail_atr_mult_after_pt1: float = 1.0
    trail_min_pts: float = 8.0
    trail_max_pts: float = 40.0
    ratchet1_frac: float = 0.5       # lock breakeven after 0.5*trail move
    level_buffer_pts: float = 2.0    # stop sits this far below a gamma level
    time_stop_hours: float = 3.0
    eod_flat_minute: int = 15 * 60 + 50   # 15:50 ET in minutes-of-day


@dataclass
class Position:
    direction: int                   # +1 long call thesis, -1 long put thesis
    entry_spot: float
    entry_premium: float             # per-contract option price at entry
    contracts: int
    entry_minute: int                # minutes-of-day at entry
    entry_level: float | None = None # the level that justified the trade

    peak_spot: float = field(init=False)
    pt1_done: bool = False
    breakeven_locked: bool = False
    remaining: int = field(init=False)

    def __post_init__(self) -> None:
        self.peak_spot = self.entry_spot
        self.remaining = self.contracts


@dataclass
class Action:
    kind: ExitReason | str           # ExitReason or "hold"
    close_contracts: int = 0
    note: str = ""


class ExitEngine:
    def __init__(self, params: CParams | None = None):
        self.p = params or CParams()

    # ── helpers ──────────────────────────────────────────────────────
    def trail_dist(self, atr30: float, pos: Position) -> float:
        m = self.p.trail_atr_mult_after_pt1 if pos.pt1_done else self.p.trail_atr_mult
        return float(min(max(m * atr30, self.p.trail_min_pts), self.p.trail_max_pts))

    def stop_level(self, pos: Position, atr30: float,
                   gamma_support: float | None) -> float:
        """Spot level at which the trail fires (long-call shown; puts mirror)."""
        dist = self.trail_dist(atr30, pos)
        raw = pos.peak_spot - pos.direction * dist
        # level-aware override: snap to just beyond a defended gamma level
        if gamma_support is not None:
            between = (raw < gamma_support < pos.peak_spot) if pos.direction > 0 \
                else (pos.peak_spot < gamma_support < raw)
            if between:
                raw = gamma_support - pos.direction * self.p.level_buffer_pts
        # ratchet 1: breakeven lock
        if pos.breakeven_locked:
            raw = max(raw, pos.entry_spot) if pos.direction > 0 else min(raw, pos.entry_spot)
        return raw

    # ── main evaluation, called once per bar/tick ────────────────────
    def evaluate(self, pos: Position, *, spot: float, option_mark: float,
                 minute_of_day: int, atr30: float,
                 gamma_support: float | None = None,
                 level_invalidated: bool = False,
                 regime_or_news_exit: bool = False) -> Action:
        p = self.p

        # update peak + ratchet arming (favorable direction only)
        if pos.direction * (spot - pos.peak_spot) > 0:
            pos.peak_spot = spot
        move = pos.direction * (pos.peak_spot - pos.entry_spot)
        if not pos.breakeven_locked and move >= p.ratchet1_frac * self.trail_dist(atr30, pos):
            pos.breakeven_locked = True

        pnl_pct = (option_mark - pos.entry_premium) / pos.entry_premium

        # ordered triggers — first wins (spec C.5)
        if regime_or_news_exit:
            return Action(ExitReason.REGIME, pos.remaining, "regime flip / news")
        if minute_of_day >= p.eod_flat_minute:
            return Action(ExitReason.EOD, pos.remaining, "15:50 unconditional flat")
        if pnl_pct <= -p.hard_stop_pct:
            return Action(ExitReason.HARD_STOP, pos.remaining,
                          f"premium {pnl_pct:+.0%} <= -{p.hard_stop_pct:.0%}")
        if level_invalidated:
            return Action(ExitReason.LEVEL_INVALID, pos.remaining,
                          "5-min close back through entry level")
        if (not pos.pt1_done) and pnl_pct >= p.pt1_pct:
            n = max(1, int(pos.remaining * p.pt1_fraction))
            pos.pt1_done = True
            pos.remaining -= n
            if pos.remaining == 0:
                return Action(ExitReason.PT1_PARTIAL, n, "PT1 (position fully closed — 1 lot)")
            return Action(ExitReason.PT1_PARTIAL, n, f"PT1 partial: sold {n}")
        stop = self.stop_level(pos, atr30, gamma_support)
        if pos.direction * (spot - stop) <= 0:
            return Action(ExitReason.TRAIL, pos.remaining,
                          f"spot {spot:.0f} through trail {stop:.0f}")
        if minute_of_day - pos.entry_minute >= p.time_stop_hours * 60 and not pos.pt1_done:
            return Action(ExitReason.TIME_STOP, pos.remaining, "3h without PT1 — thesis stale")
        return Action("hold")
