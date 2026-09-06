"""Backtest metrics & the §10 acceptance gates — the judge.

Aggregates DayResults into the numbers that decide whether ANY real dollar
ever moves: profit factor, max drawdown, cost clearance, concentration.
The gates are conjunctive: all pass or no-go.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .daysim import DayResult, TradeRecord


@dataclass
class GateParams:
    min_trades_is: int = 300
    min_trades_oos: int = 100
    min_profit_factor_oos: float = 1.3
    max_drawdown_frac: float = 0.12
    annual_fixed_costs: float = 3600.0
    top_n_removed: int = 5
    min_pf_after_removal: float = 1.15


@dataclass
class BacktestReport:
    trades: list[TradeRecord] = field(default_factory=list)
    daily_pnl: list[float] = field(default_factory=list)
    start_equity: float = 3000.0

    # ── core stats ───────────────────────────────────────────────────
    @property
    def equity_curve(self) -> np.ndarray:
        return self.start_equity + np.cumsum(np.asarray(self.daily_pnl))

    @property
    def max_drawdown_frac(self) -> float:
        eq = self.equity_curve
        if len(eq) == 0:
            return 0.0
        peak = np.maximum.accumulate(np.maximum(eq, 1e-9))
        return float(np.max((peak - eq) / peak))

    def profit_factor(self, trades: list[TradeRecord] | None = None) -> float:
        ts = self.trades if trades is None else trades
        wins = sum(t.pnl for t in ts if t.pnl > 0)
        losses = -sum(t.pnl for t in ts if t.pnl < 0)
        return float("inf") if losses == 0 else wins / losses

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return sum(1 for t in self.trades if t.pnl > 0) / len(self.trades)

    def pf_excluding_top(self, n: int) -> float:
        kept = sorted(self.trades, key=lambda t: t.pnl, reverse=True)[n:]
        return self.profit_factor(kept)

    # ── §10 gate evaluation ──────────────────────────────────────────
    def gates(self, oos: "BacktestReport", p: GateParams | None = None) -> dict:
        p = p or GateParams()
        net = float(sum(self.daily_pnl) + sum(oos.daily_pnl))
        days = max(len(self.daily_pnl) + len(oos.daily_pnl), 1)
        annualized = net * 252 / days
        checks = {
            "g1_sample_size": (len(self.trades) >= p.min_trades_is
                               and len(oos.trades) >= p.min_trades_oos,
                               f"IS {len(self.trades)}/{p.min_trades_is}, "
                               f"OOS {len(oos.trades)}/{p.min_trades_oos}"),
            "g2_oos_profit_factor": (oos.profit_factor() >= p.min_profit_factor_oos,
                                     f"OOS PF {oos.profit_factor():.2f} vs {p.min_profit_factor_oos}"),
            "g3_max_drawdown": (oos.max_drawdown_frac <= p.max_drawdown_frac,
                                f"OOS maxDD {oos.max_drawdown_frac:.1%} vs {p.max_drawdown_frac:.0%}"),
            "g4_clears_fixed_costs": (annualized > p.annual_fixed_costs,
                                      f"annualized ${annualized:,.0f} vs ${p.annual_fixed_costs:,.0f}"),
            "g5_not_concentrated": (self.pf_excluding_top(p.top_n_removed) >= p.min_pf_after_removal,
                                    f"PF-top{p.top_n_removed} "
                                    f"{self.pf_excluding_top(p.top_n_removed):.2f} vs {p.min_pf_after_removal}"),
        }
        checks["GO_LIVE_ELIGIBLE"] = (all(v[0] for v in checks.values()),
                                      "all gates green -> proceed to paper (§10.6)")
        return checks


def collect(results: list[DayResult], start_equity: float = 3000.0) -> BacktestReport:
    r = BacktestReport(start_equity=start_equity)
    for d in results:
        r.trades.extend(d.trades)
        r.daily_pnl.append(d.pnl)
    return r


def walk_forward_split(results: list[DayResult],
                       oos_frac: float = 0.25) -> tuple[list[DayResult], list[DayResult]]:
    """Chronological split: last oos_frac of days is out-of-sample. No
    shuffling — time order is the whole point."""
    cut = int(len(results) * (1 - oos_frac))
    return results[:cut], results[cut:]
