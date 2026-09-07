"""Index data via Massive REST aggregates — the fast path.

The index flat files are whole-market (~2.3 GB/day for ~25k kept rows).
The REST aggregates endpoint returns one day of I:SPX minute bars in a
single small call. Unlimited API calls on Indices Advanced makes this the
right tool; the flat-file path remains as fallback.

Output matches the flat-file parquet layout exactly (ticker/value/timestamp)
so replay.py consumes both sources identically. Manifest dataset name is
'index_values' either way — one day, one source, no double-downloads.
"""
from __future__ import annotations

import datetime as dt
import time

import httpx
import polars as pl
from rich.console import Console

from .config import settings
from .manifest import Manifest

console = Console()
BASE = "https://api.polygon.io"   # api.massive.com equivalent; both valid


def fetch_index_day(ticker: str, day: dt.date, api_key: str,
                    retries: int = 4) -> pl.DataFrame | None:
    """One day of 1-minute aggregates for one index ticker. Returns None if
    the API has no data for the day (holiday)."""
    url = (f"{BASE}/v2/aggs/ticker/{ticker}/range/1/minute/"
           f"{day}/{day}?adjusted=true&sort=asc&limit=50000&apiKey={api_key}")
    for attempt in range(retries):
        try:
            r = httpx.get(url, timeout=30)
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            results = r.json().get("results") or []
            if not results:
                return None
            return pl.DataFrame({
                "ticker": [ticker] * len(results),
                "value": [row["c"] for row in results],      # bar close as the index value
                "timestamp": [int(row["t"]) * 1_000_000 for row in results],  # ms -> ns
            })
        except httpx.HTTPError:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
    return None


def backfill_index_rest(start: dt.date, end: dt.date) -> None:
    settings.ensure_dirs()
    manifest = Manifest(settings.gex_manifest_db)          # type: ignore[arg-type]
    api_key = settings.massive_api_key
    if not api_key or api_key == "your_key_here":
        console.print("[red]MASSIVE_API_KEY not set in .env")
        return

    d = start
    while d <= end:
        if d.weekday() < 5 and not manifest.done("index_values", d):
            frames = []
            for ticker in settings.index_tickers:
                df = fetch_index_day(ticker, d, api_key)
                if df is not None:
                    frames.append(df)
            if frames:
                out = (settings.gex_parquet_dir / "index_values"     # type: ignore[operator]
                       / f"date={d}" / "data.parquet")
                out.parent.mkdir(parents=True, exist_ok=True)
                merged = pl.concat(frames)
                merged.write_parquet(out, compression="zstd")
                manifest.mark("index_values", d, "converted",
                              rows_kept=len(merged),
                              parquet_bytes=out.stat().st_size)
                console.log(f"[green]index_values {d}: {len(merged):,} rows (REST)")
            else:
                manifest.mark("index_values", d, "empty")
        d += dt.timedelta(days=1)
    console.print("[bold]Index REST backfill complete.")
