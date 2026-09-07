"""Exposure ledgers — per-strike dealer gamma/vanna/charm, and the derived
levels the strategies key off (flip point, G_MAX, walls).

Model (v0, standard/naive baseline + flow adjustment):
- OI baseline: dealers are assumed SHORT customer OI on calls sold to them?
  No — v0 uses the classic convention: dealers LONG calls, SHORT puts is
  the *old* naive model; we instead use the sign-from-flow model where the
  baseline assumes dealers are net short customer-held OI (customers net
  long options), i.e. dealer gamma = -OI_gamma for both rights, and the
  intraday flow term refines per classified trade. Both conventions are
  toggleable for the backtest to compare (§C.7 / research).
- Flow: each classified customer BUY of g gamma makes dealers shorter g;
  customer SELL makes dealers longer g.
GEX is expressed in $ gamma per 1% move: gamma * OI * 100 * S^2 * 0.01.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import numpy as np

from . import greeks


class BaselineModel(str, Enum):
    NAIVE_LONG_CALL_SHORT_PUT = "naive"     # classic GEX convention
    DEALER_SHORT_ALL = "short_all"          # customers net long everything
    FLOW_ONLY = "flow_only"                 # ignore OI, trust classified flow


@dataclass
class StrikeEntry:
    strike: float
    call_oi: float = 0.0
    put_oi: float = 0.0
    call_iv: float = 0.20
    put_iv: float = 0.20
    t_years: float = 4 / 252
    flow_gamma: float = 0.0   # dealer gamma added by today's classified flow ($ per 1% units)


@dataclass
class Ledger:
    spot: float
    baseline: BaselineModel = BaselineModel.NAIVE_LONG_CALL_SHORT_PUT
    strikes: dict[float, StrikeEntry] = field(default_factory=dict)

    # ── construction ─────────────────────────────────────────────────
    def load_oi(self, strike: float, call_oi: float, put_oi: float,
                call_iv: float, put_iv: float, t_years: float) -> None:
        self.strikes[strike] = StrikeEntry(
            strike, call_oi, put_oi, call_iv, put_iv, t_years
        )

    def on_classified_trade(self, strike: float, right: str, size: int,
                            customer_side: int, iv: float, t_years: float) -> None:
        """customer_side: +1 customer bought, -1 customer sold.
        Dealer gamma change = -customer_side * gamma_$ (dealer takes other side)."""
        e = self.strikes.setdefault(strike, StrikeEntry(strike, t_years=t_years))
        g = float(greeks.gamma(self.spot, strike, t_years, iv))
        gex_unit = g * size * 100.0 * self.spot**2 * 0.01
        e.flow_gamma += -customer_side * gex_unit

    # ── per-strike dealer GEX ────────────────────────────────────────
    def strike_gex(self, e: StrikeEntry) -> float:
        gc = float(greeks.gamma(self.spot, e.strike, e.t_years, e.call_iv))
        gp = float(greeks.gamma(self.spot, e.strike, e.t_years, e.put_iv))
        unit = 100.0 * self.spot**2 * 0.01
        if self.baseline == BaselineModel.NAIVE_LONG_CALL_SHORT_PUT:
            base = (gc * e.call_oi - gp * e.put_oi) * unit
        elif self.baseline == BaselineModel.DEALER_SHORT_ALL:
            base = -(gc * e.call_oi + gp * e.put_oi) * unit
        else:
            base = 0.0
        return base + e.flow_gamma

    # ── aggregates & levels ──────────────────────────────────────────
    def net_gex(self) -> float:
        return sum(self.strike_gex(e) for e in self.strikes.values())

    def profile(self) -> list[tuple[float, float]]:
        return sorted((k, self.strike_gex(e)) for k, e in self.strikes.items())

    def levels(self) -> dict[str, float | None]:
        prof = self.profile()
        if not prof:
            return {"flip": None, "g_max": None, "put_wall": None, "call_wall": None}
        ks = np.array([p[0] for p in prof])
        gs = np.array([p[1] for p in prof])

        # flip: zero-crossing of cumulative GEX walking up strikes
        cum = np.cumsum(gs)
        flip = None
        sign_change = np.where(np.diff(np.sign(cum)) != 0)[0]
        if len(sign_change):
            i = sign_change[0]
            flip = float(ks[i] + (ks[i + 1] - ks[i]) * abs(cum[i]) / (abs(cum[i]) + abs(cum[i + 1]) + 1e-12))

        g_max = float(ks[int(np.argmax(np.abs(gs)))])
        below, above = ks < self.spot, ks > self.spot
        put_wall = float(ks[below][int(np.argmax(np.abs(gs[below])))]) if below.any() else None
        call_wall = float(ks[above][int(np.argmax(np.abs(gs[above])))]) if above.any() else None
        return {"flip": flip, "g_max": g_max, "put_wall": put_wall, "call_wall": call_wall}


def classify_trade(trade_price: float, bid: float, ask: float) -> int:
    """Lee-Ready style: +1 customer buy (at/above mid-upper), -1 customer sell,
    0 indeterminate (exact mid)."""
    mid = 0.5 * (bid + ask)
    if trade_price > mid:
        return 1
    if trade_price < mid:
        return -1
    return 0


# ── block flow (spec C.7 question 8 — logged feature, not a trading rule) ──

from dataclasses import dataclass as _dataclass, field as _field


@_dataclass
class BlockFlowParams:
    min_contracts: int = 2000        # ⚙ SPX-scale block threshold (SPY scaled /10 upstream)
    level_proximity_pct: float = 0.0025   # "near a level" window


@_dataclass
class BlockFlowTracker:
    """Accumulates signed aggressive block flow per bar, with extra weight
    when the print lands near a computed level. Direction sign convention:
    +1 = aggressive call buying / put selling (upside pressure),
    -1 = aggressive put buying / call selling (downside pressure).

    Deliberately a MEASUREMENT, not a signal: nothing in entries/exits reads
    it. It is logged so backtest question 8 can judge whether it predicts
    anything before it is ever allowed to gate a trade.
    """
    p: BlockFlowParams = _field(default_factory=BlockFlowParams)
    by_bar: dict[int, float] = _field(default_factory=dict)
    near_level_by_bar: dict[int, float] = _field(default_factory=dict)

    def on_trade(self, *, bar_i: int, right: str, size: int, customer_side: int,
                 strike: float, spot: float,
                 levels: tuple[float | None, ...] = ()) -> None:
        if size < self.p.min_contracts or customer_side == 0:
            return
        # upside pressure: buy calls (+1,C) or sell puts (-1,P)
        direction = customer_side if right == "C" else -customer_side
        signed = direction * size
        self.by_bar[bar_i] = self.by_bar.get(bar_i, 0.0) + signed
        for lvl in levels:
            if lvl is not None and abs(strike - lvl) / max(spot, 1e-9) <= self.p.level_proximity_pct:
                self.near_level_by_bar[bar_i] = self.near_level_by_bar.get(bar_i, 0.0) + signed
                break

    def score(self, bar_i: int) -> float:
        return self.by_bar.get(bar_i, 0.0)

    def near_level_score(self, bar_i: int) -> float:
        return self.near_level_by_bar.get(bar_i, 0.0)
