"""Per-contract NBBO marks via Massive REST quotes — the honest-fills layer.

Why this exists: whole-market quote flat files are ~100 GB/day (2 B rows) and
we only ever need the 2-5 contracts the simulator actually trades. This
fetches exactly those, caches them to disk permanently, and lets every
replayed day price fills off real bid/ask instead of a flat-IV model.

Cache layout (permanent, tiny):
  parquet/rest_marks/date=YYYY-MM-DD/{OCC_TICKER}.parquet
    columns: ts_ns (Int64), bid (Float64), ask (Float64)

Usage: ReplayBuilder constructs one MarkFetcher per run; mark_fn consults
  1. the day's whole-market quote parquet (validation days — best fidelity)
  2. this REST cache / live REST call
  3. Black-Scholes fallback — ALWAYS flagged so fiction never hides.
"""
from __future__ import annotations

import datetime as dt
import time
from bisect import bisect_right
from pathlib import Path

import httpx
import polars as pl

from .clock import minute_of_day_et

BASE = "https://api.polygon.io"
SESSION_START_MIN = 9 * 60 + 30


def occ_ticker(root: str, expiry: dt.date, right: str, strike: float) -> str:
    """Build an OPRA ticker: O:XSP260612C00760000"""
    return (f"O:{root}{expiry:%y%m%d}{right}"
            f"{int(round(strike * 1000)):08d}")


class ContractQuotes:
    """Sorted NBBO series for one contract, with nearest-at-or-before lookup."""

    def __init__(self, df: pl.DataFrame):
        d = df.sort("ts_ns")
        self.minutes = [
            _ns_to_minute_of_day(t) for t in d["ts_ns"].to_list()
        ]
        self.bids = d["bid"].to_list()
        self.asks = d["ask"].to_list()

    def __len__(self) -> int:
        return len(self.minutes)

    def mark(self, minute: int) -> tuple[float, float] | None:
        i = bisect_right(self.minutes, minute) - 1
        if i < 0:
            return None
        bid, ask = self.bids[i], self.asks[i]
        if bid is None or ask is None or ask <= 0 or ask < bid:
            return None
        return (bid + ask) / 2.0, (ask - bid)


def _ns_to_minute_of_day(ts_ns: int) -> int:
    return minute_of_day_et(ts_ns)


class MarkFetcher:
    def __init__(self, cache_root: Path, api_key: str, *,
                 limit_per_call: int = 50_000, max_pages: int = 4,
                 sample_seconds: int = 30):
        self.cache_root = Path(cache_root)
        self.api_key = api_key
        self.limit = limit_per_call
        self.max_pages = max_pages
        self.sample_seconds = sample_seconds
        self._mem: dict[tuple[str, dt.date], ContractQuotes | None] = {}
        self.stats = {"cache_hits": 0, "fetched": 0, "empty": 0, "errors": 0}

    # ── cache ────────────────────────────────────────────────────────
    def _cache_path(self, ticker: str, day: dt.date) -> Path:
        safe = ticker.replace(":", "_")
        return self.cache_root / f"date={day}" / f"{safe}.parquet"

    def get(self, ticker: str, day: dt.date) -> ContractQuotes | None:
        key = (ticker, day)
        if key in self._mem:
            return self._mem[key]

        p = self._cache_path(ticker, day)
        if p.exists():
            df = pl.read_parquet(p)
            cq = ContractQuotes(df) if df.height else None
            self.stats["cache_hits"] += 1
            self._mem[key] = cq
            return cq

        df = self._fetch(ticker, day)
        p.parent.mkdir(parents=True, exist_ok=True)
        (df if df is not None else pl.DataFrame(
            {"ts_ns": [], "bid": [], "ask": []},
            schema={"ts_ns": pl.Int64, "bid": pl.Float64, "ask": pl.Float64},
        )).write_parquet(p, compression="zstd")

        cq = ContractQuotes(df) if (df is not None and df.height) else None
        self._mem[key] = cq
        return cq

    # ── REST ─────────────────────────────────────────────────────────
    def _fetch(self, ticker: str, day: dt.date) -> pl.DataFrame | None:
        """All NBBO quotes for one contract on one day, downsampled to one
        row per `sample_seconds` (marks are read at 5-min bars; storing every
        tick would waste disk for no fidelity gain)."""
        url = (f"{BASE}/v3/quotes/{ticker}"
               f"?timestamp={day}&order=asc&limit={self.limit}&apiKey={self.api_key}")
        rows: list[dict] = []
        pages = 0
        while url and pages < self.max_pages:
            try:
                r = httpx.get(url, timeout=30)
                if r.status_code == 429:
                    time.sleep(2 ** pages)
                    continue
                if r.status_code == 404:
                    self.stats["empty"] += 1
                    return None
                r.raise_for_status()
            except httpx.HTTPError:
                self.stats["errors"] += 1
                return None
            j = r.json()
            rows.extend(j.get("results") or [])
            nxt = j.get("next_url")
            url = f"{nxt}&apiKey={self.api_key}" if nxt else None
            pages += 1

        if not rows:
            self.stats["empty"] += 1
            return None
        self.stats["fetched"] += 1

        df = pl.DataFrame({
            "ts_ns": [int(r.get("sip_timestamp") or r.get("participant_timestamp") or 0)
                      for r in rows],
            "bid": [float(r.get("bid_price") or 0.0) for r in rows],
            "ask": [float(r.get("ask_price") or 0.0) for r in rows],
        }, schema={"ts_ns": pl.Int64, "bid": pl.Float64, "ask": pl.Float64})

        # downsample: keep last quote per sample bucket
        bucket = self.sample_seconds * 1_000_000_000
        return (df.filter((pl.col("ask") > 0) & (pl.col("ask") >= pl.col("bid")))
                  .with_columns((pl.col("ts_ns") // bucket).alias("_b"))
                  .group_by("_b").agg(pl.all().last())
                  .drop("_b").sort("ts_ns"))
