"""Fill & cost model (spec §10): deliberately pessimistic.

- Commission per contract per side (Schwab: $0.65).
- Entries/exits cross ⚙50% of the quoted half-spread.
- Stop-triggered exits suffer +1 tick adverse slippage on top.
If the edge survives this model, it is probably real; anything that only
works at midpoint fills does not work.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CostParams:
    commission_per_contract: float = 0.65
    half_spread_fraction: float = 0.50    # fraction of half-spread paid each side
    tick: float = 0.05                    # XSP tick
    stop_extra_ticks: int = 1
    multiplier: int = 100                 # XSP/SPX contract multiplier


class FillModel:
    def __init__(self, p: CostParams | None = None):
        self.p = p or CostParams()

    def buy(self, mid: float, spread: float, contracts: int) -> tuple[float, float]:
        """Returns (fill_price, total_cost_drag_dollars)."""
        px = mid + self.p.half_spread_fraction * (spread / 2)
        fees = self.p.commission_per_contract * contracts
        drag = (px - mid) * self.p.multiplier * contracts + fees
        return px, drag

    def sell(self, mid: float, spread: float, contracts: int,
             stop_triggered: bool = False) -> tuple[float, float]:
        px = mid - self.p.half_spread_fraction * (spread / 2)
        if stop_triggered:
            px -= self.p.stop_extra_ticks * self.p.tick
        fees = self.p.commission_per_contract * contracts
        drag = (mid - px) * self.p.multiplier * contracts + fees
        return px, drag
