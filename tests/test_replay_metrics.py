"""Replay + metrics tests. The replay fixture writes tiny Parquet files in
the EXACT layout the pipeline produces, so this doubles as a schema contract:
if the real flat files differ, these tests are where we adapt.
"""
import datetime as dt
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from gexbot.daysim import DayResult, DaySimulator, SimConfig, TradeRecord
from gexbot.metrics import BacktestReport, GateParams, collect, walk_forward_split
from gexbot.replay import ReplayBuilder
from gexbot.synth import SESSION_START

DAY = dt.date(2025, 6, 10)
ET_TO_UTC_NS = lambda mod: int((dt.datetime(2025, 6, 10, mod // 60, mod % 60,
                                            tzinfo=dt.timezone.utc)
                                - dt.timedelta(hours=-5)).timestamp() * 1e9)


@pytest.fixture()
def parquet_root(tmp_path: Path) -> Path:
    root = tmp_path / "parquet"

    # index values: SPX drifting 7590 -> 7615 over the session, 1-min prints
    rows = []
    for k, mod in enumerate(range(SESSION_START, 16 * 60)):
        rows.append({"ticker": "I:SPX", "value": 7590.0 + 25 * k / 389.0,
                     "sip_timestamp": ET_TO_UTC_NS(mod)})
    d = root / "index_values" / f"date={DAY}"
    d.mkdir(parents=True)
    pl.DataFrame(rows).write_parquet(d / "data.parquet")

    # XSP quotes for the 760C: tight book stepping with spot
    qrows = []
    for k, mod in enumerate(range(SESSION_START, 16 * 60, 2)):
        mid = 3.0 + 1.2 * k / 195.0
        qrows.append({"ticker": "O:XSP250613C00760000", "root": "XSP",
                      "strike": 760.0, "right": "C",
                      "expiry": dt.date(2025, 6, 13),
                      "bid_price": mid - 0.05, "ask_price": mid + 0.05,
                      "sip_timestamp": ET_TO_UTC_NS(mod)})
    d = root / "opra_quotes" / f"date={DAY}"
    d.mkdir(parents=True)
    pl.DataFrame(qrows).write_parquet(d / "data.parquet")

    # SPXW trades: customer put-buying at 7550 (builds a put wall), call
    # activity at 7620 (call wall) — flow-only ledger will place levels there
    trows = []
    for k, mod in enumerate(range(SESSION_START + 1, SESSION_START + 61, 2)):
        # rising put prices (upticks) = tick-rule BUY pressure at 7550;
        # rising call prices at 7620 likewise — mirrors NBBO-classified intent
        trows.append({"ticker": "O:SPXW250610P07550000", "root": "SPXW",
                      "strike": 7550.0, "right": "P",
                      "expiry": DAY, "price": 10.4 + 0.05 * k, "size": 200,
                      "sip_timestamp": ET_TO_UTC_NS(mod)})
        trows.append({"ticker": "O:SPXW250610C07620000", "root": "SPXW",
                      "strike": 7620.0, "right": "C",
                      "expiry": DAY, "price": 8.6 + 0.05 * k, "size": 150,
                      "sip_timestamp": ET_TO_UTC_NS(mod)})
    d = root / "opra_trades" / f"date={DAY}"
    d.mkdir(parents=True)
    # quotes for those SPXW contracts so classification has NBBO
    sq = []
    for mod in range(SESSION_START, SESSION_START + 62):
        sq.append({"ticker": "O:SPXW250610P07550000", "root": "SPXW",
                   "strike": 7550.0, "right": "P", "expiry": DAY,
                   "bid_price": 10.0, "ask_price": 10.5,
                   "sip_timestamp": ET_TO_UTC_NS(mod)})
        sq.append({"ticker": "O:SPXW250610C07620000", "root": "SPXW",
                   "strike": 7620.0, "right": "C", "expiry": DAY,
                   "bid_price": 8.5, "ask_price": 9.0,
                   "sip_timestamp": ET_TO_UTC_NS(mod)})
    pl.DataFrame(trows).write_parquet(d / "data.parquet")
    # append SPXW quotes into the quotes dataset
    qd = root / "opra_quotes" / f"date={DAY}" / "data.parquet"
    merged = pl.concat([pl.read_parquet(qd), pl.DataFrame(sq)], how="diagonal")
    merged.write_parquet(qd)
    return root


def test_replay_builds_bars_and_levels(parquet_root):
    rb = ReplayBuilder(parquet_root)
    day = rb.build(DAY)
    assert 75 <= len(day.bars) <= 78                      # ~78 five-min bars
    assert abs(day.bars[0].close - 7590) < 3
    pw, cw, gm = day.providers.levels_fn(20)
    # flow-built levels: put wall at the put-buying strike, call wall above
    assert pw == 7550.0
    assert cw == 7620.0


def test_replay_marks_come_from_real_quotes(parquet_root):
    rb = ReplayBuilder(parquet_root)
    day = rb.build(DAY)
    mid, spread = day.providers.mark_fn(7600.0, 760.0, SESSION_START + 30, "C")
    assert abs(spread - 0.10) < 1e-9                      # from the synthetic book
    assert 2.9 < mid < 4.5


def test_replay_runs_through_simulator(parquet_root):
    rb = ReplayBuilder(parquet_root)
    day = rb.build(DAY)
    res = DaySimulator(SimConfig(), providers=day.providers).run(day.bars)
    assert isinstance(res, DayResult)                     # end-to-end wiring
    for t in res.trades:
        assert t.exit_minute <= 15 * 60 + 55              # EOD flat holds on real path


def _fake_trade(pnl: float) -> TradeRecord:
    return TradeRecord(600, 630, 1, "bounce", 1, 5.0, 5.0 + pnl / 100, "x", pnl, 1.3)


def test_gates_judge_correctly():
    good_is = BacktestReport(trades=[_fake_trade(80)] * 240 + [_fake_trade(-50)] * 120,
                             daily_pnl=[40.0] * 120)
    good_oos = BacktestReport(trades=[_fake_trade(80)] * 80 + [_fake_trade(-50)] * 40,
                              daily_pnl=[40.0] * 40)
    g = good_is.gates(good_oos)
    assert g["g2_oos_profit_factor"][0] and g["g4_clears_fixed_costs"][0]
    assert g["GO_LIVE_ELIGIBLE"][0]

    bad_oos = BacktestReport(trades=[_fake_trade(30)] * 50 + [_fake_trade(-60)] * 60,
                             daily_pnl=[-15.0] * 40)
    g2 = good_is.gates(bad_oos)
    assert not g2["g2_oos_profit_factor"][0]
    assert not g2["GO_LIVE_ELIGIBLE"][0]                  # one red gate = no-go


def test_walk_forward_is_chronological():
    days = [DayResult() for _ in range(100)]
    is_, oos = walk_forward_split(days, oos_frac=0.25)
    assert len(is_) == 75 and len(oos) == 25
    assert oos[0] is days[75]                             # no shuffling, ever


def test_block_flow_tracker_measures_and_stays_out_of_trading():
    """Question-8 feature: blocks are measured (signed, level-weighted) but
    nothing in entries/exits consumes them — logged-only by design."""
    from gexbot.ledger import BlockFlowParams, BlockFlowTracker
    t = BlockFlowTracker(BlockFlowParams(min_contracts=1000))
    # aggressive 2k call buy near a level = +2000, counted in both scores
    t.on_trade(bar_i=3, right="C", size=2000, customer_side=1,
               strike=7550.0, spot=7552.0, levels=(7550.0, 7620.0))
    # aggressive 1.5k put buy away from levels = -1500, general only
    t.on_trade(bar_i=3, right="P", size=1500, customer_side=1,
               strike=7400.0, spot=7552.0, levels=(7550.0, 7620.0))
    # small trade ignored entirely
    t.on_trade(bar_i=3, right="C", size=50, customer_side=1,
               strike=7550.0, spot=7552.0, levels=(7550.0,))
    assert t.score(3) == 500.0                 # +2000 - 1500
    assert t.near_level_score(3) == 2000.0
    # and MarketState has no block-flow field — the wall between measurement
    # and trading is structural, not conventional
    from gexbot.entries import MarketState
    assert not any(f.startswith("block_flow") for f in MarketState.__dataclass_fields__)


def test_replay_exposes_block_flow(parquet_root):
    from gexbot.replay import ReplayBuilder
    rb = ReplayBuilder(parquet_root)
    day = rb.build(DAY)
    assert day.block_flow_by_bar is not None   # measured and carried on the day
