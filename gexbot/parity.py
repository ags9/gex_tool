"""SPX spot reconstruction via put-call parity — unlocks 2021-2022.

Massive's index history starts ~2023, but OPRA trades go back to 2021. For
European cash-settled SPX options, parity gives the forward directly:

    C - P = S*e^(-qT) - K*e^(-rT)     =>     F = (C - P) + K   (r,q ~ 0 intraday)

So for any strike where a call and a put trade near-simultaneously, the
implied forward is (C - P) + K. Aggregating many near-ATM pairs per minute
and taking a robust median yields a spot series accurate to a point or two
— plenty for level/regime work, and explicitly FLAGGED as reconstructed so
no result silently mixes provenance.

Output matches the index_values parquet schema exactly:
    ticker='I:SPX' | value | timestamp   (+ source='parity' column)
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import polars as pl
from rich.console import Console

from .config import settings
from .manifest import Manifest

console = Console()

COL_TS = "sip_timestamp"
COL_PRICE = "price"
SESSION_START = 9 * 60 + 30
SESSION_END = 16 * 60
ET_UTC_OFFSET_HOURS = -4


def _minute_expr(col: str) -> pl.Expr:
    """UTC ns -> ET minute-of-day, vectorized (no Python UDFs)."""
    secs = pl.col(col) // 1_000_000_000 + ET_UTC_OFFSET_HOURS * 3600
    return ((secs % 86400) // 60).cast(pl.Int64)


def reconstruct_day(trades_path: Path, *, roots=("SPX", "SPXW"),
                    max_pairs_per_minute: int = 40,
                    min_pairs_per_minute: int = 3) -> pl.DataFrame | None:
    """Build a 1-minute SPX series for one day from option trades."""
    lf = (
        pl.scan_parquet(trades_path)
        .filter(pl.col("root").is_in(list(roots)))
        .with_columns(_minute_expr(COL_TS).alias("mod"))
        .filter((pl.col("mod") >= SESSION_START) & (pl.col("mod") < SESSION_END))
        .select(["mod", "strike", "right", COL_PRICE, "expiry"])
    )

    # last call and last put per (minute, strike, expiry)
    calls = (lf.filter(pl.col("right") == "C")
               .group_by(["mod", "strike", "expiry"])
               .agg(pl.col(COL_PRICE).last().alias("c")))
    puts = (lf.filter(pl.col("right") == "P")
              .group_by(["mod", "strike", "expiry"])
              .agg(pl.col(COL_PRICE).last().alias("p")))

    pairs = (calls.join(puts, on=["mod", "strike", "expiry"], how="inner")
                  .with_columns(((pl.col("c") - pl.col("p")) + pl.col("strike"))
                                .alias("fwd")))

    # robust per-minute estimate: prefer pairs whose |C-P| is small (near ATM,
    # where parity is least sensitive to stale prints), then take the median
    est = (pairs
           .with_columns((pl.col("c") - pl.col("p")).abs().alias("_dist"))
           .sort(["mod", "_dist"])
           .group_by("mod")
           .agg(pl.col("fwd").head(max_pairs_per_minute).median().alias("value"),
                pl.len().alias("n_pairs"))
           .filter(pl.col("n_pairs") >= min_pairs_per_minute)
           .sort("mod")
           .collect())

    if est.height < 60:                       # need a real session
        return None

    # despike: drop points >0.5% from a 5-minute rolling median
    est = (est.with_columns(
                pl.col("value").rolling_median(5, min_samples=1).alias("_med"))
              .filter(((pl.col("value") - pl.col("_med")).abs()
                       / pl.col("_med")) < 0.005)
              .drop("_med"))

    day_date = trades_path.parent.name.split("=")[1]
    base = dt.datetime.fromisoformat(day_date).replace(tzinfo=dt.timezone.utc)
    return pl.DataFrame({
        "ticker": ["I:SPX"] * est.height,
        "value": est["value"].cast(pl.Float64),
        "timestamp": [
            int((base + dt.timedelta(minutes=int(m) - ET_UTC_OFFSET_HOURS * 60))
                .timestamp() * 1e9)
            for m in est["mod"].to_list()
        ],
        "source": ["parity"] * est.height,
    }, schema={"ticker": pl.Utf8, "value": pl.Float64,
               "timestamp": pl.Int64, "source": pl.Utf8})


def backfill_parity(start: dt.date, end: dt.date, *, overwrite: bool = False) -> None:
    settings.ensure_dirs()
    manifest = Manifest(settings.gex_manifest_db)          # type: ignore[arg-type]
    made = skipped = failed = 0
    d = start
    while d <= end:
        if d.weekday() < 5:
            out = (settings.gex_parquet_dir / "index_values"     # type: ignore[operator]
                   / f"date={d}" / "data.parquet")
            trades = (settings.gex_parquet_dir / "opra_trades"   # type: ignore[operator]
                      / f"date={d}" / "data.parquet")
            if out.exists() and not overwrite:
                skipped += 1
            elif not trades.exists():
                failed += 1
            else:
                df = reconstruct_day(trades)
                if df is None or df.is_empty():
                    manifest.mark("index_values", d, "empty")
                    failed += 1
                else:
                    out.parent.mkdir(parents=True, exist_ok=True)
                    df.write_parquet(out, compression="zstd")
                    manifest.mark("index_values", d, "converted",
                                  rows_kept=df.height,
                                  parquet_bytes=out.stat().st_size)
                    made += 1
                    console.log(f"[cyan]parity {d}: {df.height} minutes "
                                f"(open {df['value'][0]:,.0f} close {df['value'][-1]:,.0f})")
        d += dt.timedelta(days=1)
    console.print(f"[bold]Parity reconstruction: {made} built, "
                  f"{skipped} already present, {failed} unavailable")
