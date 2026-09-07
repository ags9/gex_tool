"""Backtest runner — replays a date range through ReplayBuilder + DaySimulator,
aggregates per-era metrics, evaluates the §10 gates, and writes a results
bundle (Parquet + markdown summary) designed to be reviewed in a chat or PR.

Usage (via CLI):  python -m gexbot backtest --start 2024-01-02 --end 2026-08-31
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict
from pathlib import Path

import polars as pl
from rich.console import Console
from rich.table import Table

from .config import settings
from .daysim import DaySimulator, SimConfig
from .metrics import BacktestReport, GateParams, collect, walk_forward_split
from .replay import ReplayBuilder

console = Console()


def run_backtest(start: dt.date, end: dt.date, *, tranche: float = 3000.0,
                 out_dir: Path | None = None) -> dict:
    rb = ReplayBuilder(settings.gex_parquet_dir)          # type: ignore[arg-type]
    out_dir = out_dir or (settings.gex_data_root / "results" /  # type: ignore[operator]
                          f"{dt.datetime.now():%Y%m%d_%H%M}_{start}_{end}")
    out_dir.mkdir(parents=True, exist_ok=True)

    results, day_rows, skipped = [], [], []
    d = start
    while d <= end:
        if d.weekday() < 5:
            try:
                day = rb.build(d)
                sim = DaySimulator(SimConfig(tranche=tranche),
                                   providers=day.providers)
                res = sim.run(day.bars)
                results.append(res)
                bf = day.block_flow_by_bar or {}
                bfl = day.block_flow_near_level_by_bar or {}
                day_rows.append({"day": d, "pnl": res.pnl, "trades": len(res.trades),
                                 "costs": res.total_costs, "halted": res.halted,
                                 "block_flow_net": sum(bf.values()),
                                 "block_flow_gross": sum(abs(v) for v in bf.values()),
                                 "block_flow_near_levels_net": sum(bfl.values())})
                for t in res.trades:
                    row = asdict(t); row["day"] = d
                    globals().setdefault("_trade_rows", []).append(row)
            except FileNotFoundError:
                skipped.append(str(d))
        d += dt.timedelta(days=1)

    if not results:
        console.print("[red]No replayable days in range — is index_values downloaded?")
        return {"error": "no data", "skipped_days": len(skipped)}

    trade_rows = globals().pop("_trade_rows", [])
    pl.DataFrame(day_rows).write_parquet(out_dir / "days.parquet")
    if trade_rows:
        pl.DataFrame(trade_rows).write_parquet(out_dir / "trades.parquet")

    # ── pooled + walk-forward gates ──────────────────────────────────
    is_days, oos_days = walk_forward_split(results, oos_frac=0.25)
    rep_is, rep_oos = collect(is_days, tranche), collect(oos_days, tranche)
    gates = rep_is.gates(rep_oos, GateParams())

    # ── era (yearly) split — the "is the edge decaying?" view ────────
    era_rows = []
    df_days = pl.DataFrame(day_rows)
    for (year,), g in sorted(df_days.group_by(df_days["day"].dt.year()),
                             key=lambda kv: kv[0][0]):
        yr_trades = [r for r in trade_rows if r["day"].year == year]
        wins = sum(r["pnl"] for r in yr_trades if r["pnl"] > 0)
        losses = -sum(r["pnl"] for r in yr_trades if r["pnl"] < 0)
        era_rows.append({
            "year": int(year), "days": g.height,
            "trades": len(yr_trades),
            "net_pnl": round(float(g["pnl"].sum())),
            "profit_factor": round(wins / losses, 2) if losses else float("inf"),
            "halt_days": int(g["halted"].sum()),
        })

    summary = _render(start, end, rep_is, rep_oos, gates, era_rows,
                      skipped, out_dir)
    (out_dir / "summary.md").write_text(summary)
    (out_dir / "gates.json").write_text(json.dumps(
        {k: {"passed": v[0], "detail": v[1]} for k, v in gates.items()}, indent=2))
    console.print(f"\n[bold]Results bundle:[/bold] {out_dir}")
    return {"out_dir": str(out_dir), "gates": gates, "eras": era_rows}


def _render(start, end, rep_is: BacktestReport, rep_oos: BacktestReport,
            gates: dict, era_rows: list[dict], skipped: list[str],
            out_dir: Path) -> str:
    t = Table(title=f"Era split {start} → {end}")
    for c in ("year", "days", "trades", "net_pnl", "profit_factor", "halt_days"):
        t.add_column(c)
    for r in era_rows:
        t.add_row(*(str(r[c]) for c in
                    ("year", "days", "trades", "net_pnl", "profit_factor", "halt_days")))
    console.print(t)

    g = Table(title="§10 Gates")
    g.add_column("gate"); g.add_column("pass"); g.add_column("detail")
    for k, (ok, detail) in gates.items():
        g.add_row(k, "[green]PASS" if ok else "[red]FAIL", detail)
    console.print(g)

    lines = [f"# Backtest {start} → {end}", "",
             f"IS: {len(rep_is.trades)} trades, PF {rep_is.profit_factor():.2f}, "
             f"win {rep_is.win_rate:.0%}, maxDD {rep_is.max_drawdown_frac:.1%}",
             f"OOS: {len(rep_oos.trades)} trades, PF {rep_oos.profit_factor():.2f}, "
             f"win {rep_oos.win_rate:.0%}, maxDD {rep_oos.max_drawdown_frac:.1%}", "",
             "## Era split", "",
             "| year | days | trades | net P&L | PF | halt days |", "|---|---|---|---|---|---|"]
    lines += [f"| {r['year']} | {r['days']} | {r['trades']} | ${r['net_pnl']:,} "
              f"| {r['profit_factor']} | {r['halt_days']} |" for r in era_rows]
    lines += ["", "## Gates", ""]
    lines += [f"- **{k}**: {'PASS' if ok else 'FAIL'} — {d}" for k, (ok, d) in gates.items()]
    if skipped:
        lines += ["", f"_Skipped {len(skipped)} days with missing data "
                      f"(first: {skipped[0]}, last: {skipped[-1]})_"]
    return "\n".join(lines)
