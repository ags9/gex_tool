"""Flat-file pipeline: download whole-market day file -> filter to our
underlyings -> write date-partitioned Parquet -> delete raw.

Why per-day streaming: OPRA day files cover the ENTIRE US options market.
Quotes days can be tens of GB compressed. We never keep more than one raw
day on disk (5 TB free is plenty under this policy), and the permanent
Parquet keeps only SPX/SPXW/XSP/SPY — a small fraction of the whole market.

Layout produced:
  parquet/opra_trades/date=YYYY-MM-DD/data.parquet
  parquet/opra_quotes/date=YYYY-MM-DD/data.parquet
  parquet/index_values/date=YYYY-MM-DD/data.parquet
"""
from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path

import boto3
import polars as pl
from botocore.config import Config as BotoConfig
from rich.console import Console

from .config import settings
from .symbols import root_of

console = Console()

# S3 key templates on the flat-files bucket (paths unchanged post-rebrand)
KEYS = {
    "opra_trades": "us_options_opra/trades_v1/{y}/{m:02d}/{y}-{m:02d}-{d:02d}.csv.gz",
    "opra_quotes": "us_options_opra/quotes_v1/{y}/{m:02d}/{y}-{m:02d}-{d:02d}.csv.gz",
    "index_values": "us_indices/values_v1/{y}/{m:02d}/{y}-{m:02d}-{d:02d}.csv.gz",
}


def _s3_client():
    return boto3.client(
        "s3",
        endpoint_url=settings.massive_s3_endpoint,
        aws_access_key_id=settings.massive_s3_access_key,
        aws_secret_access_key=settings.massive_s3_secret_key,
        config=BotoConfig(retries={"max_attempts": 5, "mode": "adaptive"}),
    )


def _download(s3, key: str, dest: Path) -> int | None:
    """Download one object. Returns byte size, or None if the key doesn't
    exist (holiday / not-yet-published day)."""
    try:
        head = s3.head_object(Bucket=settings.massive_s3_bucket, Key=key)
    except s3.exceptions.ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return None
        raise
    size = head["ContentLength"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    s3.download_file(settings.massive_s3_bucket, key, str(tmp))
    tmp.rename(dest)
    return size


def _filter_options_day(raw: Path, out: Path, roots: set[str]) -> int:
    """Stream-filter a whole-market OPRA csv.gz down to our roots -> Parquet.

    Polars' lazy CSV scanner streams gzip without materializing the file.
    We add parsed symbol columns here so the backtest never re-parses.
    """
    lf = pl.scan_csv(raw, infer_schema_length=10_000)
    lf = (
        lf.with_columns(
            pl.col("ticker")
            .map_elements(root_of, return_dtype=pl.Utf8)
            .alias("root")
        )
        .filter(pl.col("root").is_in(list(roots)))
        .with_columns(
            # tail slicing mirrors symbols.parse_option_ticker
            pl.col("ticker").str.slice(-15, 6).str.strptime(pl.Date, "%y%m%d").alias("expiry"),
            pl.col("ticker").str.slice(-9, 1).alias("right"),
            (pl.col("ticker").str.slice(-8, 8).cast(pl.Int64) / 1000.0).alias("strike"),
        )
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    lf.sink_parquet(out, compression="zstd", statistics=True)
    return pl.scan_parquet(out).select(pl.len()).collect().item()


def _filter_index_day(raw: Path, out: Path, tickers: list[str]) -> int:
    lf = pl.scan_csv(raw, infer_schema_length=10_000).filter(
        pl.col("ticker").is_in(tickers)
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    lf.sink_parquet(out, compression="zstd", statistics=True)
    return pl.scan_parquet(out).select(pl.len()).collect().item()


def process_day(s3, manifest, dataset: str, day: dt.date) -> None:
    if manifest.done(dataset, day):
        return
    key = KEYS[dataset].format(y=day.year, m=day.month, d=day.day)
    raw = settings.gex_raw_dir / dataset / f"{day}.csv.gz"          # type: ignore[operator]
    out = settings.gex_parquet_dir / dataset / f"date={day}" / "data.parquet"  # type: ignore[operator]

    raw_bytes = _download(_s3_client() if s3 is None else s3, key, raw)
    if raw_bytes is None:
        manifest.mark(dataset, day, "empty")
        return
    try:
        if dataset.startswith("opra"):
            rows = _filter_options_day(raw, out, set(settings.underlyings))
        else:
            rows = _filter_index_day(raw, out, settings.index_tickers)
        manifest.mark(
            dataset, day, "converted",
            rows_kept=rows, raw_bytes=raw_bytes,
            parquet_bytes=out.stat().st_size,
        )
        console.log(f"[green]{dataset} {day}: {rows:,} rows kept "
                    f"({raw_bytes/1e9:.1f} GB raw -> {out.stat().st_size/1e6:.0f} MB)")
    except Exception as e:  # mark failed, keep going; rerun retries it
        manifest.mark(dataset, day, "failed", error=str(e))
        console.log(f"[red]{dataset} {day} FAILED: {e}")
        if out.exists():
            out.unlink()
    finally:
        raw.unlink(missing_ok=True)  # never keep raw whole-market files


def trading_days(start: dt.date, end: dt.date):
    d = start
    while d <= end:
        if d.weekday() < 5:  # exchange holidays resolve as 'empty' via 404
            yield d
        d += dt.timedelta(days=1)


def free_space_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / 1e9
