"""Day simulator — replays Strategy C over one session of 5-min bars.

Binds: EntryEngine (C.4/C.9/C.11) + ExitEngine (C.5/C.5a) + DisciplineState
(C.6) + FillModel (§10) + Black-Scholes marks. One bar loop, one trade log.

Option marks are BS-priced from spot/time (IV held constant per day in v0;
real backtest will use actual quote data — this simulator's job is to make
the LOGIC correct and measurable before the data lands).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import greeks
from .costs import CostParams, FillModel
from .discipline import DisciplineParams, DisciplineState
from .entries import EntryEngine, EntryParams, MarketState
from .exits import CParams, ExitEngine, ExitReason, Position
from .synth import Bar, atr30


@dataclass
class SimConfig:
    tranche: float = 3000.0
    premium_budget_frac: float = 0.40
    dte_years: float = 4 / 252
    iv: float = 0.16
    xsp_divisor: float = 10.0            # XSP = SPX/10
    quoted_spread: float = 0.10          # XSP option spread (mid ± half)
    put_wall: float = 7550.0
    call_wall: float = 7660.0
    g_max: float = 7595.0
    regime_by_bar: list[str] | None = None   # override; default all "P"


@dataclass
class Providers:
    """Injectable real-data hooks (historical replay / live). Any None falls
    back to SimConfig synthetic behavior — one simulator, three harnesses
    (synthetic, backtest, live), which is what makes signal-parity checks
    meaningful.
      mark_fn(spot, strike, minute, right) -> (mid, spread)
      levels_fn(bar_index) -> (put_wall, call_wall, g_max)
      regimes: per-bar regime list
    """
    mark_fn: object | None = None
    levels_fn: object | None = None
    regimes: list[str] | None = None


@dataclass
class TradeRecord:
    entry_minute: int
    exit_minute: int
    direction: int
    kind: str
    contracts: int
    entry_fill: float
    exit_fill: float
    exit_reason: str
    pnl: float
    cost_drag: float


@dataclass
class DayResult:
    trades: list[TradeRecord] = field(default_factory=list)
    blocked: list = field(default_factory=list)
    halted: bool = False
    halt_reason: str = ""

    @property
    def pnl(self) -> float:
        return sum(t.pnl for t in self.trades)

    @property
    def total_costs(self) -> float:
        return sum(t.cost_drag for t in self.trades)


class DaySimulator:
    def __init__(self, cfg: SimConfig | None = None,
                 entry_params: EntryParams | None = None,
                 exit_params: CParams | None = None,
                 disc_params: DisciplineParams | None = None,
                 cost_params: CostParams | None = None,
                 providers: 'Providers | None' = None):
        self.cfg = cfg or SimConfig()
        self.entries = EntryEngine(entry_params)
        self.exits = ExitEngine(exit_params)
        self.disc = DisciplineState(disc_params or DisciplineParams(),
                                    tranche_equity_open=self.cfg.tranche)
        self.fills = FillModel(cost_params)
        self.prov = providers or Providers()

    # ── option pricing helpers (XSP scale) ───────────────────────────
    def _strike_for(self, spot: float, direction: int) -> float:
        xsp = spot / self.cfg.xsp_divisor
        return (float(int(xsp)) + (1.0 if direction > 0 else 0.0))  # nearest ATM-ish

    def _mark(self, spot: float, strike: float, minute: int, right: str) -> tuple[float, float]:
        if self.prov.mark_fn is not None:
            return self.prov.mark_fn(spot, strike, minute, right)
        t = self.cfg.dte_years - (minute - (9 * 60 + 30)) / (390.0 * 252.0)
        mid = float(greeks.price(spot / self.cfg.xsp_divisor, strike, max(t, 1e-6),
                                 self.cfg.iv, right))
        return mid, self.cfg.quoted_spread

    # ── market state construction per bar ────────────────────────────
    def _mkt(self, bars: list[Bar], i: int, session_high: float,
             regime: str) -> MarketState:
        b, prev = bars[i], bars[i - 1] if i else bars[i]
        c = self.cfg
        if self.prov.levels_fn is not None:
            pw, cw, gm = self.prov.levels_fn(i)
        else:
            pw, cw, gm = c.put_wall, c.call_wall, c.g_max
        self._cur_levels = (pw, cw, gm)
        near_support = min(
            (lvl for lvl in (pw, gm) if lvl is not None and lvl < b.close),
            key=lambda l: b.close - l, default=None)
        bounce = (near_support is not None
                  and b.low <= near_support * 1.0012
                  and b.close > near_support and b.close > b.open)
        breakdown = pw is not None and prev.close > pw >= b.close
        # bars since new session high
        bsnh = 0
        for j in range(i, -1, -1):
            if bars[j].high >= session_high - 1e-9:
                break
            bsnh += 1
        return MarketState(
            minute=b.minute, spot=b.close, session_high=session_high,
            regime=regime, put_wall=pw, call_wall=cw,
            g_max=gm, bar_close_above_level=bounce,
            bar_break_below_level=breakdown, option_spread=c.quoted_spread,
            news_blocked=False, bars_since_new_high=bsnh,
            lower_high_close_below_prior_low=(b.high < prev.high and b.close < prev.low),
        )

    # ── main loop ────────────────────────────────────────────────────
    def run(self, bars: list[Bar]) -> DayResult:
        cfg, res = self.cfg, DayResult()
        regimes = self.prov.regimes or cfg.regime_by_bar or ["P"] * len(bars)
        pos: Position | None = None
        pos_meta: dict = {}
        session_high = bars[0].high

        for i, b in enumerate(bars):
            session_high = max(session_high, b.high)
            m = self._mkt(bars, i, session_high, regimes[i])
            a30 = atr30(bars, i)

            if pos is not None:
                right = "C" if pos.direction > 0 else "P"
                mark, spr = self._mark(b.close, pos_meta["strike"], b.minute, right)
                regime_exit = (m.regime != "P" and pos_meta["kind"] in ("bounce", "flip", "reversal"))
                act = self.exits.evaluate(
                    pos, spot=b.close * pos.direction if False else b.close,
                    option_mark=mark, minute_of_day=b.minute, atr30=a30,
                    gamma_support=(self._cur_levels[2] if pos.direction > 0 else None),
                    regime_or_news_exit=regime_exit)
                if act.kind != "hold":
                    stop_hit = act.kind in (ExitReason.HARD_STOP, ExitReason.TRAIL)
                    fill, drag = self.fills.sell(mark, spr,
                                                 act.close_contracts,
                                                 stop_triggered=(act.kind == ExitReason.HARD_STOP))
                    pnl = (fill - pos_meta["entry_fill"]) * 100 * act.close_contracts \
                        - self.fills.p.commission_per_contract * act.close_contracts
                    res.trades.append(TradeRecord(
                        pos.entry_minute, b.minute, pos.direction, pos_meta["kind"],
                        act.close_contracts, pos_meta["entry_fill"], fill,
                        str(act.kind), pnl, drag))
                    fully_closed = (act.kind != ExitReason.PT1_PARTIAL) or pos.remaining == 0
                    if not fully_closed:
                        self.disc.on_partial(pnl_dollars=pnl)
                    if fully_closed:
                        self.disc.on_exit(
                            pnl_dollars=pnl,
                            was_stopout=(act.kind == ExitReason.HARD_STOP),
                            was_trail=(act.kind == ExitReason.TRAIL),
                            was_flip=(pos_meta["kind"] == "flip"),
                            minute=b.minute)
                        # C.9: try the flip on a profitable level exit
                        flip = None
                        if act.kind in (ExitReason.TRAIL, ExitReason.PT1_PARTIAL) and pnl > 0:
                            flip = self.entries.flip_trigger(
                                m, pos.direction, True, not self.disc.flip_mode_dead)
                        pos = None
                        if flip is not None and not self.disc.halted:
                            ok, why = self.disc.may_enter(
                                minute=b.minute, direction=flip.direction,
                                is_reentry_after_trail=False,
                                is_range_day=True)
                            if ok:
                                pos, pos_meta = self._open(flip, b, res)
                    continue

            if pos is None and not self.disc.halted:
                sig = self.entries.evaluate(m)
                if sig is not None:
                    ok, why = self.disc.may_enter(
                        minute=b.minute, direction=sig.direction,
                        is_reentry_after_trail=False, is_range_day=True)
                    if ok:
                        pos, pos_meta = self._open(sig, b, res)
                    else:
                        res.blocked.append((b.minute, sig.kind, why))

        res.halted, res.halt_reason = self.disc.halted, self.disc.halt_reason
        res.blocked.extend((mn, bs.would_be, bs.blocked_by)
                           for mn, bs in self.entries.blocked_log)
        return res

    def _open(self, sig, b: Bar, res: DayResult):
        cfg = self.cfg
        right = "C" if sig.direction > 0 else "P"
        strike = self._strike_for(b.close, sig.direction)
        mid, spr = self._mark(b.close, strike, b.minute, right)
        budget = cfg.premium_budget_frac * cfg.tranche
        contracts = max(1, int(budget / (mid * 100)))
        fill, drag = self.fills.buy(mid, spr, contracts)
        self.disc.on_entry(direction=sig.direction, is_reentry_after_trail=False)
        pos = Position(direction=sig.direction, entry_spot=b.close,
                       entry_premium=fill, contracts=contracts,
                       entry_minute=b.minute, entry_level=sig.level)
        return pos, {"strike": strike, "kind": sig.kind, "entry_fill": fill}
