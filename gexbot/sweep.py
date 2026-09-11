"""Parameter sweep — runs the breakout backtest across a grid and reports
every combination's gates, so §9 changes are made from a table rather than
a hunch.

Design notes:
- Sweeps reuse the cached REST marks, so a 12-point grid over 4 years costs
  minutes, not hours.
- Reports PLATEAUS, not peaks: a parameter value that only works at one
  exact setting is overfit. The summary flags neighbours' agreement.
- Never writes config. It prints evidence; the human edits the versioned
  parameter file afterward (§9).

Usage:
    python -m gexbot sweep --start 2023-01-03 --end 2026-09-04 \
        --param premium_budget_frac --values 0.15,0.20,0.25,0.30,0.40
"""
from __future__ import annotations

import datetime as dt
import os
from dataclasses import replace
from pathlib import Path

import polars as pl
from rich.console import Console
from rich.table import Table

from .config import settings
from .daysim import DaySimulator, SimConfig
from .entries import EntryParams
from .exits import CParams
from .metrics import GateParams, collect, walk_forward_split
from .replay import ReplayBuilder

console = Console()


def _build_days(start: dt.date, end: dt.date, rb: ReplayBuilder) -> list:
    """Replay each day ONCE; reuse the built days across every grid point.
    Ledger construction is the expensive part — sweeping only re-runs the
    (cheap) simulator."""
    days = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            try:
                days.append(rb.build(d))
            except FileNotFoundError:
                pass
        d += dt.timedelta(days=1)
    return days


def run_sweep(start: dt.date, end: dt.date, param: str, values: list[float],
              *, tranche: float = 3000.0, breakout_only: bool = True,
              max_dd: float | None = None) -> None:
    """max_dd: the OOS max-drawdown threshold every grid point is judged at.
    Defaults to GateParams' own 12%; round-two runs pass 0.20 per
    PREREGISTRATION §4. Recorded per row so the saved parquet always says
    which threshold produced its gates_passed count."""
    from dotenv import load_dotenv
    load_dotenv()
    from .marks_rest import MarkFetcher

    rb = ReplayBuilder(settings.gex_parquet_dir)          # type: ignore[arg-type]
    key = os.getenv("MASSIVE_API_KEY", "")
    if key and key != "your_key_here":
        rb.mark_fetcher = MarkFetcher(
            Path(settings.gex_parquet_dir) / "rest_marks", key)   # type: ignore[operator]

    gp_base = GateParams() if max_dd is None else GateParams(max_drawdown_frac=max_dd)
    console.print(f"[bold]Gates judged at OOS maxDD <= "
                  f"{gp_base.max_drawdown_frac:.1%}, OOS PF >= "
                  f"{gp_base.min_profit_factor_oos}")
    console.print(f"[bold]Building days {start} → {end} (once, reused across grid)…")
    days = _build_days(start, end, rb)
    console.print(f"  {len(days)} replayable sessions\n")

    rows = []
    for v in values:
        sim_kw: dict = {"tranche": tranche}
        exit_p = CParams()
        entry_p = EntryParams(breakout_only=breakout_only)

        if param in SimConfig.__dataclass_fields__:
            sim_kw[param] = v
        elif param in CParams.__dataclass_fields__:
            exit_p = replace(exit_p, **{param: v})
        elif param in EntryParams.__dataclass_fields__:
            entry_p = replace(entry_p, **{param: v})
        else:
            raise SystemExit(f"unknown parameter: {param}")

        results = []
        for day in days:
            sim = DaySimulator(SimConfig(**sim_kw), entry_params=entry_p,
                               exit_params=exit_p, providers=day.providers)
            results.append(sim.run(day.bars))

        is_days, oos_days = walk_forward_split(results, oos_frac=0.25)
        rep_is, rep_oos = collect(is_days, tranche), collect(oos_days, tranche)
        gates = rep_is.gates(rep_oos, gp_base)
        net = sum(sum(r.pnl for r in results) for _ in (0,)) / 1
        rows.append({
            "value": v,
            "trades": len(rep_is.trades) + len(rep_oos.trades),
            "net_pnl": round(net),
            "pf_all": round(rep_is.profit_factor(), 2),
            "pf_oos": round(rep_oos.profit_factor(), 2),
            "maxdd_oos": round(rep_oos.max_drawdown_frac * 100, 1),
            "maxdd_gate": gp_base.max_drawdown_frac,
            "annualized": round(net * 252 / max(len(days), 1)),
            "gates_passed": sum(1 for k, g in gates.items()
                                if k != "GO_LIVE_ELIGIBLE" and g[0]),
        })
        console.log(f"{param}={v}: net ${rows[-1]['net_pnl']:,} "
                    f"OOS PF {rows[-1]['pf_oos']} DD {rows[-1]['maxdd_oos']}%")

    t = Table(title=f"Sweep: {param}   ({start} → {end}, "
                    f"{'breakout-only' if breakout_only else 'full strategy'}, "
                    f"maxDD gate {gp_base.max_drawdown_frac:.1%})")
    for c in ("value", "trades", "net_pnl", "pf_all", "pf_oos",
              "maxdd_oos", "maxdd_gate", "annualized", "gates_passed"):
        t.add_column(c)
    for r in rows:
        t.add_row(*(str(r[c]) for c in
                    ("value", "trades", "net_pnl", "pf_all", "pf_oos",
                     "maxdd_oos", "maxdd_gate", "annualized", "gates_passed")))
    console.print(t)

    # plateau check — the anti-overfit read
    best = max(rows, key=lambda r: r["gates_passed"] * 100 + r["pf_oos"])
    i = rows.index(best)
    neighbours = [rows[j] for j in (i - 1, i + 1) if 0 <= j < len(rows)]
    if neighbours and all(n["pf_oos"] >= 1.0 for n in neighbours):
        console.print(f"[green]PLATEAU: {param}={best['value']} sits among "
                      f"neighbours that also work — safe candidate.")
    else:
        console.print(f"[yellow]PEAK (not plateau): {param}={best['value']} "
                      f"outperforms neighbours that fail — likely overfit; "
                      f"prefer a value inside a stable region.")

    out = Path(settings.gex_data_root) / "results" / f"sweep_{param}_{start}_{end}.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows).write_parquet(out)
    console.print(f"[bold]Saved: {out}")
