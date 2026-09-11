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

from .clock import minute_of_day_et, minute_of_day_expr
from .daysim import Providers
from .marks_rest import MarkFetcher, occ_ticker
from .ledger import (BaselineModel, BlockFlowTracker, Ledger, classify_trade)
from .synth import BAR_MIN, SESSION_START, Bar

# flat-file column names (single place to adapt if schema differs)
COL_TS = "sip_timestamp"        # ns since epoch in OPRA (options) files
COL_TS_IDX = "timestamp"        # index flat files use plain "timestamp"
COL_PRICE = "price"
COL_SIZE = "size"
COL_BID = "bid_price"
COL_ASK = "ask_price"
IDX_TICKER = "I:SPX"


@dataclass
class ReplayDay:
    day: dt.date
    bars: list[Bar]
    providers: Providers
    block_flow_by_bar: dict[int, float] | None = None
    block_flow_near_level_by_bar: dict[int, float] | None = None


class QuoteBook:
    """Nearest-at-or-before quote lookup for one (root,strike,right,expiry)
    contract, from the day's quotes parquet. Marks come from REAL NBBO —
    the whole point of the replay."""

    def __init__(self, quotes: pl.DataFrame):
        q = quotes.sort(COL_TS)
        self.minutes = [minute_of_day_et(t) for t in q[COL_TS]]
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
        self.mark_fetcher: MarkFetcher | None = None
        self.fallback_marks = 0
        self.rest_marks = 0
        self.file_marks = 0

    # ── loaders ──────────────────────────────────────────────────────
    def _read(self, dataset: str, day: dt.date) -> pl.DataFrame | None:
        p = self.root / dataset / f"date={day}" / "data.parquet"
        return pl.read_parquet(p) if p.exists() else None

    def _scan(self, dataset: str, day: dt.date) -> pl.LazyFrame | None:
        p = self.root / dataset / f"date={day}" / "data.parquet"
        return pl.scan_parquet(p) if p.exists() else None

    def spx_bars(self, day: dt.date) -> list[Bar]:
        df = self._read("index_values", day)
        if df is None or df.is_empty():
            raise FileNotFoundError(f"no index_values for {day}")
        ts_col = COL_TS_IDX if COL_TS_IDX in df.columns else COL_TS
        df = df.filter(pl.col("ticker") == IDX_TICKER).with_columns(
            minute_of_day_expr(ts_col).alias("mod")
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
                      oi_path: Path | None = None) -> tuple[list[tuple], BlockFlowTracker]:
        trades = self._read("opra_trades", day)
        led = Ledger(spot=bars[0].close, baseline=self.baseline)
        t_years = 4 / 252
        blocks = BlockFlowTracker()

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
        if trades is not None and not trades.is_empty():
            # TICK-RULE classification (spec pivot): sign of each trade vs the
            # prior trade in the same contract, zero-ticks inherit the last
            # nonzero direction. Needs trades only — the 2B-row quote join
            # that OOM-killed 16GB machines is gone from the ledger path.
            # NBBO-based classification remains available for validation-day
            # studies via a direct script; question: measured accuracy delta.
            tq = (trades.lazy()
                  .filter(pl.col("root").is_in(self.spx_roots))
                  .sort("ticker", COL_TS)
                  .with_columns(
                      pl.col(COL_PRICE).diff().over("ticker").alias("_d"))
                  .with_columns(
                      pl.when(pl.col("_d") > 0).then(1)
                        .when(pl.col("_d") < 0).then(-1)
                        .otherwise(None).alias("_side"))
                  .with_columns(
                      pl.col("_side").forward_fill().over("ticker")
                        .fill_null(0).alias("side"))
                  .select(["strike", "right", COL_SIZE, COL_TS, "side"])
                  .collect())
            for r in tq.iter_rows(named=True):
                mod = minute_of_day_et(r[COL_TS])
                if not (SESSION_START <= mod < 16 * 60):
                    continue
                bar_i = (mod - SESSION_START) // BAR_MIN
                side = int(r["side"])
                if side:
                    flow_by_bar.setdefault(bar_i, []).append(
                        (float(r["strike"]), r["right"], int(r[COL_SIZE]), side))

        out: list[tuple] = []
        cached = None
        regimes: list[str] = []
        run_scale = 1.0
        _pending = None; _pend_n = 0; _current = "X"
        for i, b in enumerate(bars):
            led.spot = b.close
            for strike, right, size, side in flow_by_bar.get(i, []):
                led.on_classified_trade(strike, right, size, side,
                                        iv=0.18, t_years=t_years)
                blocks.on_trade(bar_i=i, right=right, size=size,
                                customer_side=side, strike=strike,
                                spot=b.close,
                                levels=cached if cached else ())
            if cached is None or i % self.level_refresh_bars == 0:
                lv = led.levels()
                cached = (lv["put_wall"], lv["call_wall"], lv["g_max"])
            out.append(cached)
            net = led.net_gex()
            run_scale = max(run_scale, abs(net))
            cand = "X" if abs(net) < 0.05 * run_scale else ("P" if net > 0 else "N")
            if cand == _current:
                _pending, _pend_n = None, 0
            elif cand == _pending:
                _pend_n += 1
                if _pend_n >= (1 if cand == "N" else 2):
                    _current, _pending, _pend_n = cand, None, 0
            else:
                _pending, _pend_n = cand, 1
            regimes.append(_current)
        self._last_regimes = regimes
        return out, blocks

    # ── assemble a full replay day ───────────────────────────────────
    def build(self, day: dt.date, *, oi_path: Path | None = None) -> ReplayDay:
        bars = self.spx_bars(day)
        if len(bars) < 12:
            # holiday / half-session / stray after-hours prints: not a
            # replayable session. Same treatment as a missing file.
            raise FileNotFoundError(f"{day}: only {len(bars)} session bars")
        levels, blocks = self.levels_by_bar(day, bars, oi_path=oi_path)
        quotes_lf = self._scan("opra_quotes", day)

        books: dict[tuple, QuoteBook] = {}

        # choose the replay contract's expiry once per day: nearest listed
        # XSP expiry >= 3 calendar days out (C.2: 3-4 DTE)
        expiries = None
        if quotes_lf is not None:
            expiries = sorted(
                quotes_lf.filter(pl.col("root") == self.xsp_root)
                         .select("expiry").unique().collect()["expiry"].to_list())
        target_expiry = None
        if expiries:
            candidates = [e for e in expiries if (e - day).days >= 3]
            target_expiry = candidates[0] if candidates else expiries[-1]
        else:
            # No whole-market quote file for this day (the normal case): pick
            # the expiry by CALENDAR rule instead, so REST marks still work.
            # XSP lists Mon/Wed/Fri expiries; C.2 wants 3-4 DTE.
            d = day + dt.timedelta(days=3)
            for _ in range(7):
                if d.weekday() in (0, 2, 4):      # Mon/Wed/Fri
                    target_expiry = d
                    break
                d += dt.timedelta(days=1)

        def mark_fn(spot: float, strike: float, minute: int, right: str):
            key = (strike, right)
            if key not in books and quotes_lf is not None:
                # nearest listed XSP expiry >= 3 trading days is chosen upstream
                # in v0 we take the front root match on strike/right
                flt = ((pl.col("root") == self.xsp_root)
                       & (pl.col("strike") == strike) & (pl.col("right") == right))
                if target_expiry is not None:
                    flt = flt & (pl.col("expiry") == target_expiry)
                qdf = (quotes_lf.filter(flt)
                       .collect(engine="streaming"))
                books[key] = QuoteBook(qdf) if not qdf.is_empty() else None
            book = books.get(key)
            m = book.mark(minute) if book else None
            if m is not None:
                self.file_marks += 1
                return m
            # 2) REST per-contract NBBO (cached) — real fills without the
            #    100 GB/day whole-market quote files
            if self.mark_fetcher is not None and target_expiry is not None:
                tkr = occ_ticker(self.xsp_root, target_expiry, right, strike)
                cq = self.mark_fetcher.get(tkr, day)
                if cq is not None:
                    m2 = cq.mark(minute)
                    if m2 is not None:
                        self.rest_marks += 1
                        return m2
            # fallback: BS mark (contract had no quotes — flagged for audit)
            self.fallback_marks += 1
            from . import greeks
            t = max(4 / 252 - (minute - SESSION_START) / (390.0 * 252.0), 1e-6)
            return float(greeks.price(spot / 10.0, strike, t, 0.16, right)), 0.10

        def levels_fn(i: int):
            return levels[min(i, len(levels) - 1)]

        return ReplayDay(day=day, bars=bars,
                         providers=Providers(mark_fn=mark_fn, levels_fn=levels_fn,
                                             regimes=getattr(self, '_last_regimes', None)),
                         block_flow_by_bar=blocks.by_bar,
                         block_flow_near_level_by_bar=blocks.near_level_by_bar)
