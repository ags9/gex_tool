"""Discipline state machine (spec C.6) — the anti-revenge machinery.

Tracks, per session: trade count, consecutive stop-outs, lockout windows,
flip-loss streaks, daily P&L breaker. Pure logic: the simulator and the
live engine both consult `may_enter()` before ANY entry. There is no
override method — by design.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DisciplineParams:
    stopout_lockout_min: int = 30
    max_losing_trades_per_day: int = 2
    max_trades_per_day: int = 3
    max_trades_range_day: int = 5        # C.9 cap when flip-mode qualifies
    reentry_bars_after_trail: int = 1    # C.5b: one full 5-min bar
    max_same_dir_reentries: int = 2      # C.5b
    daily_loss_halt_frac: float = 0.15   # -15% of tranche -> done
    max_consec_losing_flips: int = 2     # C.9 kill


@dataclass
class DisciplineState:
    p: DisciplineParams = field(default_factory=DisciplineParams)
    tranche_equity_open: float = 3000.0

    trades_today: int = 0
    losing_trades_today: int = 0
    realized_pnl_today: float = 0.0
    locked_until_minute: int = -1
    last_trail_exit_minute: int = -10_000
    same_dir_reentries: dict[int, int] = field(default_factory=lambda: {1: 0, -1: 0})
    consec_losing_flips: int = 0
    flip_mode_dead: bool = False
    halted: bool = False
    halt_reason: str = ""

    # ── event hooks (simulator/live engine call these) ───────────────
    def on_exit(self, *, pnl_dollars: float, was_stopout: bool,
                was_trail: bool, was_flip: bool, minute: int) -> None:
        self.trades_today += 1
        self.realized_pnl_today += pnl_dollars
        if pnl_dollars < 0:
            self.losing_trades_today += 1
        if was_stopout:
            self.locked_until_minute = minute + self.p.stopout_lockout_min
        if was_trail:
            self.last_trail_exit_minute = minute
        if was_flip:
            self.consec_losing_flips = self.consec_losing_flips + 1 if pnl_dollars < 0 else 0
            if self.consec_losing_flips >= self.p.max_consec_losing_flips:
                self.flip_mode_dead = True
        self._check_halts()

    def _check_halts(self) -> None:
        if self.realized_pnl_today <= -self.p.daily_loss_halt_frac * self.tranche_equity_open:
            self.halted, self.halt_reason = True, "daily loss breaker (-15% tranche)"
        elif self.losing_trades_today >= self.p.max_losing_trades_per_day:
            self.halted, self.halt_reason = True, "2 losing trades — done for the day"

    # ── the gate ─────────────────────────────────────────────────────
    def may_enter(self, *, minute: int, direction: int,
                  is_reentry_after_trail: bool, is_range_day: bool) -> tuple[bool, str]:
        if self.halted:
            return False, self.halt_reason
        if minute < self.locked_until_minute:
            return False, f"stop-out lockout until {self.locked_until_minute // 60:02d}:{self.locked_until_minute % 60:02d}"
        cap = self.p.max_trades_range_day if is_range_day else self.p.max_trades_per_day
        if self.trades_today >= cap:
            return False, f"daily trade cap ({cap}) reached"
        if is_reentry_after_trail:
            if minute - self.last_trail_exit_minute < 5 * self.p.reentry_bars_after_trail:
                return False, "C.5b: wait one full bar after trail exit"
            if self.same_dir_reentries[direction] >= self.p.max_same_dir_reentries:
                return False, "C.5b: same-direction re-entry cap (chasing)"
        return True, "ok"

    def on_entry(self, *, direction: int, is_reentry_after_trail: bool) -> None:
        if is_reentry_after_trail:
            self.same_dir_reentries[direction] += 1
