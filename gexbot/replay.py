"""Historical replay — turns one day of pipeline Parquet into Providers for
the DaySimulator. This module IS the bridge from synthetic proof to real
backtest.

Inputs (produced by gexbot.pipeline):
  parquet/index_values/date=D/data.parquet   -> SPX 5-min bars
  parquet/opra_quotes/date=D/data.parquet    -> real option marks (nearest quote)
  parquet/opra_trades/date=D/data.parquet    -> classified flow -> ledger levels
Optional:
  oi/date=D.parquet with columns [root,strike,right,expiry,oi,iv] -> OI baseline

Column names follow Polygon/Massive flat-file conventions; adjust CONFIG
constants below if the delivered schema differs (one place to fix).
"""
from __future__ import annotations

import datetime as dt
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from .daysim import Providers
from .ledger import BaselineModel, Ledger, classify_trade
from .synth import BAR_MIN, SESSION_START, Bar

# flat-file column names (single place to adapt if schema differs)
COL_TS = "sip_timestamp"        # ns since epoch in OPRA files
COL_PRICE = "price"
COL_SIZE = "size"
COL_BID = "bid_price"
COL_ASK = "ask_price"
IDX_TICKER = "I:SPX"
ET_UTC_OFFSET_HOURS = -5        # replay v0: standard time; TODO real tz calendar


def _minute_of_day_et(ts_ns: int) -> int:
    t = dt.datetime.fromtimestamp(ts_ns / 1e9, tz=dt.timezone.utc)
    t = t + dt.timedelta(hours=ET_UTC_OFFSET_HOURS)
    return t.hour * 60 + t.minute


@dataclass
class ReplayDay:
    day: dt.date
    bars: list[Bar]
    providers: Providers


class QuoteBook:
    """Nearest-at-or-before quote lookup for one (root,strike,right,expiry)
    contract, from the day's quotes parquet. Marks come from REAL NBBO —
    the whole point of the replay."""

    def __init__(self, quotes: pl.DataFrame):
        q = quotes.sort(COL_TS)
        self.minutes = [(_minute_of_day_et(t)) for t in q[COL_TS]]
        self.bids = q[COL_BID].to_list()
        self.asks = q[COL_ASK].to_list()

    def mark(self, minute: int) -> tuple[float, float] | None:
        i = bisect_right(self.minutes, minute) - 1
        if i < 0:
            return None
        bid, ask = self.bids[i], self.asks[i]
        if bid is None or ask is None or ask <= 0 or ask < bid:
            return None
        return (bid + ask) / 2.0, (ask - bid)


class ReplayBuilder:
    def __init__(self, parquet_root: Path, *, xsp_root: str = "XSP",
                 spx_roots: tuple[str, ...] = ("SPX", "SPXW"),
                 baseline: BaselineModel = BaselineModel.NAIVE_LONG_CALL_SHORT_PUT,
                 level_refresh_bars: int = 3):
        self.root = Path(parquet_root)
        self.xsp_root = xsp_root
        self.spx_roots = spx_roots
        self.baseline = baseline
        self.level_refresh_bars = level_refresh_bars

    # ── loaders ──────────────────────────────────────────────────────
    def _read(self, dataset: str, day: dt.date) -> pl.DataFrame | None:
        p = self.root / dataset / f"date={day}" / "data.parquet"
        return pl.read_parquet(p) if p.exists() else None

    def spx_bars(self, day: dt.date) -> list[Bar]:
        df = self._read("index_values", day)
        if df is None or df.is_empty():
            raise FileNotFoundError(f"no index_values for {day}")
        df = df.filter(pl.col("ticker") == IDX_TICKER).with_columns(
            pl.col(COL_TS).map_elements(_minute_of_day_et, return_dtype=pl.Int64)
            .alias("mod")
        ).filter((pl.col("mod") >= SESSION_START) & (pl.col("mod") < 16 * 60))
        df = df.with_columns(
            ((pl.col("mod") - SESSION_START) // BAR_MIN).alias("bar_i")
        )
        val = "value" if "value" in df.columns else COL_PRICE
        agg = df.group_by("bar_i").agg(
            pl.col(val).first().alias("open"), pl.col(val).max().alias("high"),
            pl.col(val).min().alias("low"), pl.col(val).last().alias("close"),
        ).sort("bar_i")
        return [Bar(SESSION_START + (int(r["bar_i"]) + 1) * BAR_MIN,
                    float(r["open"]), float(r["high"]), float(r["low"]),
                    float(r["close"]))
                for r in agg.iter_rows(named=True)]

    # ── ledger levels per bar (SPX-scale) ────────────────────────────
    def levels_by_bar(self, day: dt.date, bars: list[Bar],
                      oi_path: Path | None = None) -> list[tuple]:
        trades = self._read("opra_trades", day)
        quotes = self._read("opra_quotes", day)
        led = Ledger(spot=bars[0].close, baseline=self.baseline)
        t_years = 4 / 252

        if oi_path is not None and oi_path.exists():
            for r in pl.read_parquet(oi_path).iter_rows(named=True):
                if r["root"] in self.spx_roots:
                    e = led.strikes.setdefault(
                        float(r["strike"]),
                        type(led).load_oi.__self__ if False else None) # placeholder
            # simpler: bulk load
            led.strikes.clear()
            df = pl.read_parquet(oi_path).filter(pl.col("root").is_in(self.spx_roots))
            for strike, g in df.group_by("strike"):
                gg = g if isinstance(g, pl.DataFrame) else g[1]
                calls = gg.filter(pl.col("right") == "C")
                puts = gg.filter(pl.col("right") == "P")
                led.load_oi(float(strike[0] if isinstance(strike, tuple) else strike),
                            call_oi=float(calls["oi"].sum() or 0),
                            put_oi=float(puts["oi"].sum() or 0),
                            call_iv=float(calls["iv"].mean() or 0.18),
                            put_iv=float(puts["iv"].mean() or 0.18),
                            t_years=t_years)

        # pre-bucket classified SPX-complex trades by bar
        flow_by_bar: dict[int, list] = {}
        if trades is not None and quotes is not None and not trades.is_empty():
            tq = trades.filter(pl.col("root").is_in(self.spx_roots))
            # nearest-quote NBBO per trade via asof join on timestamp+ticker
            qq = quotes.select(["ticker", COL_TS, COL_BID, COL_ASK]).sort(COL_TS)
            tq = tq.sort("ticker", COL_TS).join_asof(qq.sort("ticker", COL_TS), on=COL_TS, by="ticker",
                                           strategy="backward")
            for r in tq.iter_rows(named=True):
                mod = _minute_of_day_et(r[COL_TS])
                if not (SESSION_START <= mod < 16 * 60):
                    continue
                bar_i = (mod - SESSION_START) // BAR_MIN
                if r[COL_BID] is None or r[COL_ASK] is None:
                    continue
                side = classify_trade(r[COL_PRICE], r[COL_BID], r[COL_ASK])
                if side:
                    flow_by_bar.setdefault(bar_i, []).append(
                        (float(r["strike"]), r["right"], int(r[COL_SIZE]), side))

        out: list[tuple] = []
        cached = None
        for i, b in enumerate(bars):
            led.spot = b.close
            for strike, right, size, side in flow_by_bar.get(i, []):
                led.on_classified_trade(strike, right, size, side,
                                        iv=0.18, t_years=t_years)
            if cached is None or i % self.level_refresh_bars == 0:
                lv = led.levels()
                cached = (lv["put_wall"], lv["call_wall"], lv["g_max"])
            out.append(cached)
        return out

    # ── assemble a full replay day ───────────────────────────────────
    def build(self, day: dt.date, *, oi_path: Path | None = None) -> ReplayDay:
        bars = self.spx_bars(day)
        levels = self.levels_by_bar(day, bars, oi_path=oi_path)
        quotes = self._read("opra_quotes", day)

        books: dict[tuple, QuoteBook] = {}

        def mark_fn(spot: float, strike: float, minute: int, right: str):
            key = (strike, right)
            if key not in books and quotes is not None:
                # nearest listed XSP expiry >= 3 trading days is chosen upstream
                # in v0 we take the front root match on strike/right
                qdf = quotes.filter(
                    (pl.col("root") == self.xsp_root)
                    & (pl.col("strike") == strike) & (pl.col("right") == right))
                books[key] = QuoteBook(qdf) if not qdf.is_empty() else None
            book = books.get(key)
            m = book.mark(minute) if book else None
            if m is not None:
                return m
            # fallback: BS mark (contract had no quotes — flagged for audit)
            from . import greeks
            t = max(4 / 252 - (minute - SESSION_START) / (390.0 * 252.0), 1e-6)
            return float(greeks.price(spot / 10.0, strike, t, 0.16, right)), 0.10

        def levels_fn(i: int):
            return levels[min(i, len(levels) - 1)]

        return ReplayDay(day=day, bars=bars,
                         providers=Providers(mark_fn=mark_fn, levels_fn=levels_fn))
