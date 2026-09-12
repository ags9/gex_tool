"""CLI:  python -m gexbot backfill [--dataset opra_trades] [--start 2023-01-01]

Runs the resumable backfill. Ctrl-C safe: at most the current day repeats.
Order of datasets is deliberate — trades first (smaller, unlocks most
research), quotes second (the big one), indices last (tiny).
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys

from rich.console import Console
from rich.table import Table

from .config import settings
from .manifest import Manifest
from .metrics import GateParams
from .pipeline import KEYS, _s3_client, free_space_gb, process_day, trading_days

console = Console()
MIN_FREE_GB = 200  # halt if the drive gets this low; one raw quotes day can be huge
DD_HELP = (
    "OOS max-drawdown gate threshold as a fraction. Default is the GateParams "
    "default (%(default)s); round-two evidence runs pass 0.20 per "
    "PREREGISTRATION section 4. Recorded in the results bundle."
)


def backfill(datasets: list[str], start: dt.date, end: dt.date) -> None:
    settings.ensure_dirs()
    manifest = Manifest(settings.gex_manifest_db)  # type: ignore[arg-type]
    s3 = _s3_client()
    for dataset in datasets:
        console.rule(f"[bold]{dataset}  {start} → {end}")
        for day in trading_days(start, end):
            if free_space_gb(settings.gex_data_root) < MIN_FREE_GB:
                console.log(f"[red]Free space under {MIN_FREE_GB} GB — halting safely.")
                sys.exit(1)
            process_day(s3, manifest, dataset, day)
    _summary(manifest)


def _summary(manifest: Manifest) -> None:
    t = Table(title="Pipeline manifest")
    for col in ("dataset", "state", "days", "rows", "GB parquet"):
        t.add_column(col)
    for row in manifest.summary():
        t.add_row(*(f"{v:,.1f}" if isinstance(v, float) else f"{v}" for v in row))
    console.print(t)


def main() -> None:
    p = argparse.ArgumentParser(prog="gexbot")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("backfill", help="download+convert flat files")
    b.add_argument("--dataset", choices=list(KEYS), action="append",
                   help="repeatable; default = all three in sensible order")
    b.add_argument("--start", type=dt.date.fromisoformat, default=settings.gex_start_date)
    b.add_argument("--end", type=dt.date.fromisoformat, default=settings.end_date)

    sub.add_parser("status", help="print manifest summary")

    sub.add_parser("discord-test", help="send a test message to each configured webhook")

    bt = sub.add_parser("backtest", help="replay a date range through Strategy C and report gates")
    bt.add_argument("--start", type=dt.date.fromisoformat, required=True)
    bt.add_argument("--end", type=dt.date.fromisoformat, required=True)
    bt.add_argument("--tranche", type=float, default=3000.0)
    bt.add_argument("--max-dd", type=float, default=GateParams().max_drawdown_frac,
                    help=DD_HELP)

    ir = sub.add_parser("index-rest", help="fast index backfill via REST aggregates (no 2.3GB flat files)")
    ir.add_argument("--start", type=dt.date.fromisoformat, default=settings.gex_start_date)
    ir.add_argument("--end", type=dt.date.fromisoformat, default=settings.end_date)

    pm = sub.add_parser("premarket",
                        help="overnight range vs the last stored map; posts to #daily")
    pm.add_argument("--date", type=dt.date.fromisoformat, default=None)
    pm.add_argument("--dry-run", action="store_true",
                    help="print the summary without posting to Discord")

    ap = sub.add_parser("api", help="serve the read-only state API (localhost only)")
    ap.add_argument("--port", type=int, default=settings.gex_api_port,
                    help="default %(default)s (GEX_API_PORT)")

    wt = sub.add_parser("watch", help="market-hours structural alerts + shadow trade narration")
    wt.add_argument("--underlying", default="I:SPX", choices=["I:SPX", "SPY"],
                    help="the instrument that ALERTS (default %(default)s)")
    wt.add_argument("--complex", dest="complex_map", action="store_true",
                    help="also compute and store the other instrument and the "
                         "merged S&P complex book, in parallel and SILENT — "
                         "the combined map never alerts (spec §10.3)")
    wt.add_argument("--interval", type=int, default=180, help="seconds between polls")
    wt.add_argument("--expiries", type=int, default=2)
    wt.add_argument("--window", type=float, default=0.06)
    wt.add_argument("--tranche", type=float, default=3000.0)
    wt.add_argument("--no-shadow", action="store_true",
                    help="structural alerts only, no shadow trades")
    wt.add_argument("--record-tape", action="store_true",
                    help="also write every classified print to Parquet under "
                         "tape/date=…. Unbounded — for one investigation, not "
                         "a default")
    wt.add_argument("--no-flow", action="store_true",
                    help="disable the live flow overlay; map reverts to the "
                         "OI baseline, which is blind to 0DTE")
    wt.add_argument("--once", action="store_true", help="single poll then exit (test)")

    lv = sub.add_parser("levels", help="print today's GEX map (chain snapshot)")
    lv.add_argument("--underlying", default="I:SPX")
    lv.add_argument("--model", choices=["naive", "short_all"], default="naive")
    lv.add_argument("--expiries", type=int, default=2,
                    help="how many nearest expiries to aggregate")
    lv.add_argument("--per-1pct", action="store_true",
                    help="quote $GEX per 1%% move instead of per point")
    lv.add_argument("--window", type=float, default=0.06,
                    help="strike window as fraction of spot")

    ct = sub.add_parser("control", help="null-model test: does the GEX entry beat random?")
    ct.add_argument("--start", type=dt.date.fromisoformat, required=True)
    ct.add_argument("--end", type=dt.date.fromisoformat, required=True)
    ct.add_argument("--seeds", type=int, default=20)
    ct.add_argument("--tranche", type=float, default=3000.0)

    pa = sub.add_parser("parity", help="reconstruct SPX spot from option trades (2021-22 out-of-sample)")
    pa.add_argument("--start", type=dt.date.fromisoformat, required=True)
    pa.add_argument("--end", type=dt.date.fromisoformat, required=True)
    pa.add_argument("--overwrite", action="store_true",
                    help="rebuild days that already have index data")

    sw = sub.add_parser("sweep", help="run a parameter sweep and report the gate frontier")
    sw.add_argument("--start", type=dt.date.fromisoformat, required=True)
    sw.add_argument("--end", type=dt.date.fromisoformat, required=True)
    sw.add_argument("--param", required=True,
                    help="e.g. premium_budget_frac | trail_atr_mult | pt1_pct | hard_stop_pct")
    sw.add_argument("--values", required=True, help="comma-separated, e.g. 0.15,0.20,0.30")
    sw.add_argument("--tranche", type=float, default=3000.0)
    sw.add_argument("--full-strategy", action="store_true",
                    help="sweep with all books enabled (default: breakout-only)")
    sw.add_argument("--max-dd", type=float, default=GateParams().max_drawdown_frac,
                    help=DD_HELP)

    args = p.parse_args()
    if args.cmd == "status":
        _summary(Manifest(settings.gex_manifest_db))  # type: ignore[arg-type]
    elif args.cmd == "discord-test":
        from dotenv import load_dotenv  # type: ignore
        load_dotenv()
        from .notify import Channel, Color, from_settings
        n = from_settings()
        if not n.webhooks:
            console.print("[red]No webhook URLs configured in .env")
            return
        n.send(Channel.TRADES, "Test — trades channel", "Entry/exit messages will appear here.", Color.BLUE)
        n.breaker(name="Test — alerts channel", detail="Circuit breakers and warnings will ping here.")
        n.daily_digest(body="Test — daily digests will appear here.", green_day=True)
        n.flush()
        console.print(f"[green]Sent test messages to {len(n.webhooks)} configured channel(s).")
    elif args.cmd == "backtest":
        from .backtest import run_backtest
        run_backtest(args.start, args.end, tranche=args.tranche,
                     max_dd=args.max_dd)
    elif args.cmd == "sweep":
        from .sweep import run_sweep
        run_sweep(args.start, args.end, args.param,
                  [float(v) for v in args.values.split(",")],
                  tranche=args.tranche, breakout_only=not args.full_strategy,
                  max_dd=args.max_dd)
    elif args.cmd == "parity":
        from .parity import backfill_parity
        backfill_parity(args.start, args.end, overwrite=args.overwrite)
    elif args.cmd == "control":
        from .control import run_control
        run_control(args.start, args.end, seeds=args.seeds, tranche=args.tranche)
    elif args.cmd == "levels":
        from dotenv import load_dotenv  # type: ignore
        load_dotenv()
        from .levels import show_levels
        show_levels(args.underlying, args.model, args.expiries, args.window,
                    per_point=not args.per_1pct)
    elif args.cmd == "watch":
        from dotenv import load_dotenv  # type: ignore
        load_dotenv()
        from .watch import run_watch
        try:
            run_watch(underlying=args.underlying, interval=args.interval,
                  expiries=args.expiries, window=args.window,
                  tranche=args.tranche, shadow=not args.no_shadow,
                      once=args.once, flow=not args.no_flow,
                      complex_map=args.complex_map,
                      record_tape=args.record_tape)
        except KeyboardInterrupt:
            console.print("\n[yellow]watch stopped (state saved).")
    elif args.cmd == "premarket":
        from .premarket import run_premarket
        raise SystemExit(run_premarket(day=args.date, dry_run=args.dry_run))
    elif args.cmd == "api":
        from .api import run_api
        run_api(port=args.port)
    elif args.cmd == "index-rest":
        from dotenv import load_dotenv  # type: ignore
        load_dotenv()
        from .index_rest import backfill_index_rest
        backfill_index_rest(args.start, args.end)
    else:
        datasets = args.dataset or ["opra_trades", "opra_quotes", "index_values"]
        backfill(datasets, args.start, args.end)


if __name__ == "__main__":
    main()
