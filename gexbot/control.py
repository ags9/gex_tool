"""Control experiment — does the GEX entry logic beat chance?

The question every strategy must answer before parameter tuning means
anything: are the levels informative, or are we just harvesting whatever
edge exists in "be long/short SPX options intraday with these exits"?

Method: run the SAME simulator, SAME exits, SAME sizing, SAME number of
trades per day, but replace the entry TRIGGER with matched-random entries:

  MATCHED_TIME      real entry minutes, random direction
  MATCHED_DIR       random minutes in the entry window, real direction mix
  FULL_RANDOM       random minutes, random direction
  SHUFFLED_LEVELS   real trigger logic, but levels shuffled between days
                    (the sharpest test: same market, wrong walls)

If the strategy's P&L distribution is inside the random distribution, the
levels add nothing and no amount of tuning will help. If it sits clearly
outside, the levels carry signal and the work is worth continuing.

Runs N seeds per arm to get a distribution, not a point estimate.
"""
from __future__ import annotations

import datetime as dt
import os
import random
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import polars as pl
from rich.console import Console
from rich.table import Table

from .config import settings
from .daysim import DaySimulator, Providers, SimConfig
from .entries import EntryParams
from .exits import CParams
from .replay import ReplayBuilder

console = Console()

ARMS = ("strategy", "matched_time", "matched_dir", "full_random", "shuffled_levels")


@dataclass
class ArmResult:
    arm: str
    seed: int
    net_pnl: float
    trades: int
    profit_factor: float


def _pf(trades) -> float:
    w = sum(t.pnl for t in trades if t.pnl > 0)
    l = -sum(t.pnl for t in trades if t.pnl < 0)
    return float("inf") if l == 0 else w / l


class RandomEntryProviders:
    """Wraps a day's providers, but forces entries at chosen minutes."""

    def __init__(self, base: Providers, forced: list[tuple[int, int]]):
        self.base = base
        self.forced = dict(forced)          # minute -> direction


def run_control(start: dt.date, end: dt.date, *, seeds: int = 20,
                tranche: float = 3000.0,
                entry_params: EntryParams | None = None,
                exit_params: CParams | None = None) -> None:
    from dotenv import load_dotenv
    load_dotenv()
    from .marks_rest import MarkFetcher

    rb = ReplayBuilder(settings.gex_parquet_dir)          # type: ignore[arg-type]
    key = os.getenv("MASSIVE_API_KEY", "")
    if key and key != "your_key_here":
        rb.mark_fetcher = MarkFetcher(
            Path(settings.gex_parquet_dir) / "rest_marks", key)   # type: ignore[operator]

    console.print(f"[bold]Building days {start} → {end}…")
    days = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            try:
                days.append(rb.build(d))
            except FileNotFoundError:
                pass
        d += dt.timedelta(days=1)
    console.print(f"  {len(days)} sessions\n")
    if not days:
        return

    ep = entry_params or EntryParams(breakout_only=True)
    xp = exit_params or CParams()

    # ── arm 1: the real strategy (deterministic, one run) ────────────
    real_trades, real_pnl = [], 0.0
    day_signature: list[tuple[int, int]] = []   # (n_trades, dominant direction)
    for day in days:
        res = DaySimulator(SimConfig(tranche=tranche), entry_params=ep,
                           exit_params=xp, providers=day.providers).run(day.bars)
        real_trades.extend(res.trades)
        real_pnl += res.pnl
        entries = {(t.entry_minute, t.direction) for t in res.trades}
        day_signature.append((len(entries),
                              max((t.direction for t in res.trades), default=1)))
    rows = [ArmResult("strategy", 0, real_pnl, len(real_trades), _pf(real_trades))]
    console.log(f"[bold cyan]strategy: net ${real_pnl:,.0f} "
                f"({len(real_trades)} trade legs, PF {_pf(real_trades):.2f})")

    # ── arms 2-5: randomized entries, matched trade counts ───────────
    n_per_day = [s[0] for s in day_signature]
    real_dirs = [t.direction for t in real_trades] or [1]
    real_minutes = sorted({t.entry_minute for t in real_trades}) or [11 * 60]

    for arm in ARMS[1:]:
        for seed in range(seeds):
            rng = random.Random(hash((arm, seed)) & 0xFFFFFFFF)
            trades, pnl = [], 0.0

            # shuffled-levels arm: rotate level providers between days
            if arm == "shuffled_levels":
                idx = list(range(len(days)))
                rng.shuffle(idx)

            for i, day in enumerate(days):
                if arm == "shuffled_levels":
                    donor = days[idx[i]]
                    # donor days differ in length — pad/clamp to this day's bars
                    dr = list(donor.providers.regimes or [])
                    n = len(day.bars)
                    dr = (dr + [dr[-1] if dr else "X"] * n)[:n]
                    _dlev = donor.providers.levels_fn
                    prov = Providers(mark_fn=day.providers.mark_fn,
                                     levels_fn=lambda i, _f=_dlev: _f(i),
                                     regimes=dr)
                    res = DaySimulator(SimConfig(tranche=tranche), entry_params=ep,
                                       exit_params=xp, providers=prov).run(day.bars)
                    trades.extend(res.trades)
                    pnl += res.pnl
                    continue

                # forced-entry arms: same trade count, randomized when/which way
                k = n_per_day[i]
                if k == 0:
                    continue
                res = _forced_entry_day(day, k, arm, rng, real_minutes,
                                        real_dirs, tranche, xp)
                trades.extend(res[0])
                pnl += res[1]

            rows.append(ArmResult(arm, seed, pnl, len(trades), _pf(trades)))
        arm_rows = [r for r in rows if r.arm == arm]
        nets = np.array([r.net_pnl for r in arm_rows])
        console.log(f"{arm}: median ${np.median(nets):,.0f}  "
                    f"p5 ${np.percentile(nets,5):,.0f}  p95 ${np.percentile(nets,95):,.0f}")

    _report(rows, real_pnl, start, end, len(days))


def _forced_entry_day(day, k: int, arm: str, rng, real_minutes, real_dirs,
                      tranche: float, xp: CParams):
    """Enter k times at random minutes/directions; manage with the real exit
    engine so only the ENTRY TRIGGER differs from the strategy."""
    from .exits import ExitEngine, ExitReason, Position
    from .costs import FillModel

    fills = FillModel()
    eng = ExitEngine(xp)
    bars = day.bars
    window = [b for b in bars if 9 * 60 + 45 <= b.minute <= 14 * 60 + 30]
    if not window:
        return [], 0.0

    if arm == "matched_time":
        picks = rng.sample(real_minutes, min(k, len(real_minutes)))
        dirs = [rng.choice([1, -1]) for _ in picks]
    elif arm == "matched_dir":
        picks = [rng.choice(window).minute for _ in range(k)]
        dirs = [rng.choice(real_dirs) for _ in picks]
    else:
        picks = [rng.choice(window).minute for _ in range(k)]
        dirs = [rng.choice([1, -1]) for _ in picks]

    trades, total = [], 0.0
    for minute, direction in zip(picks, dirs):
        bar_i = next((j for j, b in enumerate(bars) if b.minute >= minute), None)
        if bar_i is None:
            continue
        b0 = bars[bar_i]
        strike = float(int(b0.close / 10.0)) + (1.0 if direction > 0 else 0.0)
        right = "C" if direction > 0 else "P"
        mid, spr = day.providers.mark_fn(b0.close, strike, b0.minute, right)
        if mid <= 0.05:
            continue
        contracts = max(1, int(0.25 * tranche / (mid * 100)))
        entry_fill, _ = fills.buy(mid, spr, contracts)
        pos = Position(direction=direction, entry_spot=b0.close,
                       entry_premium=entry_fill, contracts=contracts,
                       entry_minute=b0.minute)
        for b in bars[bar_i + 1:]:
            m, s = day.providers.mark_fn(b.close, strike, b.minute, right)
            from .synth import atr30
            act = eng.evaluate(pos, spot=b.close, option_mark=m,
                               minute_of_day=b.minute,
                               atr30=atr30(bars, bars.index(b)))
            if act.kind != "hold":
                exit_fill, _ = fills.sell(m, s, act.close_contracts,
                                          stop_triggered=(act.kind == ExitReason.HARD_STOP))
                pnl = ((exit_fill - entry_fill) * 100 * act.close_contracts
                       - 0.65 * act.close_contracts)
                total += pnl
                trades.append(type("T", (), {"pnl": pnl})())
                if act.kind != ExitReason.PT1_PARTIAL or pos.remaining == 0:
                    break
    return trades, total


def _report(rows: list[ArmResult], real_pnl: float, start, end, n_days: int) -> None:
    t = Table(title=f"Control experiment  {start} → {end}  ({n_days} sessions)")
    for c in ("arm", "runs", "median_net", "p5", "p95", "median_pf",
              "P(random ≥ strategy)"):
        t.add_column(c)

    verdicts = {}
    for arm in ARMS:
        arm_rows = [r for r in rows if r.arm == arm]
        nets = np.array([r.net_pnl for r in arm_rows])
        pfs = np.array([r.profit_factor for r in arm_rows if np.isfinite(r.profit_factor)])
        if arm == "strategy":
            t.add_row(arm, "1", f"${nets[0]:,.0f}", "—", "—",
                      f"{pfs[0]:.2f}" if len(pfs) else "—", "—")
            continue
        p_beat = float((nets >= real_pnl).mean())
        verdicts[arm] = p_beat
        t.add_row(arm, str(len(arm_rows)), f"${np.median(nets):,.0f}",
                  f"${np.percentile(nets,5):,.0f}", f"${np.percentile(nets,95):,.0f}",
                  f"{np.median(pfs):.2f}" if len(pfs) else "—",
                  f"{p_beat:.0%}")
    console.print(t)

    worst = max(verdicts.values()) if verdicts else 1.0
    if worst <= 0.05:
        console.print("[bold green]SIGNAL: the strategy beats every random arm "
                      "at p ≤ 0.05. The levels carry information.")
    elif worst <= 0.20:
        console.print("[yellow]WEAK/AMBIGUOUS: strategy is above most random runs "
                      "but not decisively. More data or a sharper entry needed.")
    else:
        console.print("[bold red]NO SIGNAL: random entries match or beat the "
                      "strategy. The GEX entry trigger is not adding value — "
                      "tuning exits will not fix this.")

    out = Path(settings.gex_data_root) / "results" / f"control_{start}_{end}.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame([r.__dict__ for r in rows]).write_parquet(out)
    console.print(f"[bold]Saved: {out}")
