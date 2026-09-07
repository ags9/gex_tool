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
from .pipeline import KEYS, _s3_client, free_space_gb, process_day, trading_days

console = Console()
MIN_FREE_GB = 200  # halt if the drive gets this low; one raw quotes day can be huge


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

    ir = sub.add_parser("index-rest", help="fast index backfill via REST aggregates (no 2.3GB flat files)")
    ir.add_argument("--start", type=dt.date.fromisoformat, default=settings.gex_start_date)
    ir.add_argument("--end", type=dt.date.fromisoformat, default=settings.end_date)

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
        run_backtest(args.start, args.end, tranche=args.tranche)
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
