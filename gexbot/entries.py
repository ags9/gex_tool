"""Entry engine (spec C.4, C.9 flip trigger, C.11 reversal score).

Pure logic like exits.py: fed market state per bar, emits EntrySignal or
None plus the reason every gate blocked. Logging blocked signals is not
decoration — it's the §11/§14.3 'filter effectiveness' dataset.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class EntryParams:
    # C.4
    level_proximity_pct: float = 0.0010     # within 0.10% of level for bounce
    entry_start_min: int = 9 * 60 + 45
    entry_end_min: int = 14 * 60 + 30
    max_spread_xsp: float = 0.20
    # C.9
    flip_level_proximity_pct: float = 0.0015
    flip_min_range_pct: float = 0.006       # walls at least 0.6% apart
    stall_bars_for_rolloff: int = 3
    # C.11
    reversal_enable_score: int = 70
    reversal_veto_score: int = 30
    breakout_only: bool = False
    selloff_threshold_pct: float = 0.0075   # score computed when down >= 0.75%


@dataclass
class MarketState:
    """One evaluation snapshot (5-min cadence in the simulator)."""
    minute: int
    spot: float
    session_high: float
    regime: str                      # "P" | "N" | "X"
    put_wall: float | None
    call_wall: float | None
    g_max: float | None
    bar_close_above_level: bool      # confirmed bounce bar (computed upstream)
    bar_break_below_level: bool      # confirmed breakdown bar
    option_spread: float             # candidate contract's bid/ask width
    news_blocked: bool               # severity>=2 recent, tripwire, blackout
    # momentum rolloff inputs (C.9b)
    bars_since_new_high: int
    lower_high_close_below_prior_low: bool
    # macro (C.11)
    usdjpy_flat_or_up_60m: bool = True
    yield_stable: bool = True
    no_recent_sev2_90m: bool = True


@dataclass
class EntrySignal:
    direction: int                   # +1 call, -1 put
    kind: str                        # "bounce" | "breakout" | "flip" | "reversal"
    level: float
    note: str = ""


@dataclass
class BlockedSignal:
    would_be: str
    blocked_by: str


class EntryEngine:
    def __init__(self, params: EntryParams | None = None):
        self.p = params or EntryParams()
        self.blocked_log: list[tuple[int, BlockedSignal]] = []

    # ── C.11 reversal score ──────────────────────────────────────────
    def reversal_score(self, m: MarketState) -> int | None:
        drawdown = (m.session_high - m.spot) / m.session_high
        if drawdown < self.p.selloff_threshold_pct:
            return None
        score = 0
        score += 30 if m.usdjpy_flat_or_up_60m else 0
        score += 25 if m.yield_stable else 0
        score += 25 if m.no_recent_sev2_90m else 0
        if m.put_wall is not None and abs(m.spot - m.put_wall) / m.spot <= 0.0020:
            score += 20
        return score

    # ── shared gates ─────────────────────────────────────────────────
    def _gates(self, m: MarketState, would_be: str) -> str | None:
        if not (self.p.entry_start_min <= m.minute <= self.p.entry_end_min):
            return "time window"
        if m.news_blocked:
            return "news/vol/blackout gate"
        if m.option_spread > self.p.max_spread_xsp:
            return "liquidity gate"
        return None

    def _near(self, spot: float, level: float | None, pct: float) -> bool:
        return level is not None and abs(spot - level) / spot <= pct

    # ── main evaluation ──────────────────────────────────────────────
    def evaluate(self, m: MarketState) -> EntrySignal | None:
        # C.11 veto: macro-sponsored selloff blocks all long-call dip buys
        score = self.reversal_score(m)
        veto_calls = score is not None and score <= self.p.reversal_veto_score

        candidates: list[EntrySignal] = []
        # EXPERIMENT (⚙ breakout_only): disable mean-reversion books to test
        # the four-year finding that breakout is the only consistently green
        # strategy. Set via EntryParams; default False keeps full behavior.
        _bo = getattr(self.p, "breakout_only", False)

        # C.4a bounce (REGIME_P): pullback to support + confirmed bounce bar
        if (not _bo) and m.regime == "P" and m.bar_close_above_level:
            for lvl in (m.put_wall, m.g_max):
                if lvl is not None and lvl < m.spot and self._near(m.spot, lvl, self.p.level_proximity_pct):
                    kind = "reversal" if (score is not None and score >= self.p.reversal_enable_score) else "bounce"
                    candidates.append(EntrySignal(+1, kind, lvl, f"bounce at {lvl:.0f}"))
                    break

        # C.4b breakout (REGIME_N): confirmed break of a level
        if m.regime == "N" and m.bar_break_below_level and m.put_wall is not None:
            candidates.append(EntrySignal(-1, "breakout", m.put_wall,
                                          f"breakdown through {m.put_wall:.0f}"))

        for sig in candidates:
            if sig.direction == +1 and veto_calls:
                self.blocked_log.append((m.minute, BlockedSignal(sig.kind, "C.11 macro veto")))
                continue
            blocked = self._gates(m, sig.kind)
            if blocked:
                self.blocked_log.append((m.minute, BlockedSignal(sig.kind, blocked)))
                continue
            return sig
        return None

    # ── C.9 flip trigger (called while a position is open) ───────────
    def flip_trigger(self, m: MarketState, position_direction: int,
                     position_profitable: bool, flip_mode_alive: bool) -> EntrySignal | None:
        if not flip_mode_alive or m.regime != "P" or not position_profitable:
            return None
        if m.put_wall is None or m.call_wall is None:
            return None
        if (m.call_wall - m.put_wall) / m.spot < self.p.flip_min_range_pct:
            return None
        opposing = m.call_wall if position_direction > 0 else m.put_wall
        if not self._near(m.spot, opposing, self.p.flip_level_proximity_pct):
            return None
        rolloff = (m.lower_high_close_below_prior_low
                   or m.bars_since_new_high >= self.p.stall_bars_for_rolloff)
        if not rolloff:
            return None
        blocked = self._gates(m, "flip")
        if blocked:
            self.blocked_log.append((m.minute, BlockedSignal("flip", blocked)))
            return None
        return EntrySignal(-position_direction, "flip", opposing,
                           f"curl at {opposing:.0f} — flip {'put' if position_direction>0 else 'call'}")
