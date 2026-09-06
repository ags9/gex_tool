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

    args = p.parse_args()
    if args.cmd == "status":
        _summary(Manifest(settings.gex_manifest_db))  # type: ignore[arg-type]
    else:
        datasets = args.dataset or ["opra_trades", "opra_quotes", "index_values"]
        backfill(datasets, args.start, args.end)


if __name__ == "__main__":
    main()
