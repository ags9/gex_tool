"""The classified tape (spec §15).

The panel's reason for existing is diagnostic, so these tests are mostly about
the diagnostic columns and the bound — not about display.
"""
import datetime as dt
import time
from pathlib import Path

import duckdb
import pytest

from gexbot.livefeed import FlowLedger, OptionsFeed, TapeBuffer, TapePrint
from gexbot.state import TAPE_CAP, StateStore

GAMMA = 0.0028
EXP = "260914"


def mk(i: int = 0, *, side: int = 1, size: int = 5, gamma: float = GAMMA,
       delta: float = -100.0) -> TapePrint:
    return TapePrint(ts=time.time() + i * 1e-3, ticker="O:SPXW260914C07650000",
                     root="SPXW", expiry=EXP, strike=7650.0, right="C",
                     price=10.0 + i, size=size, side=side,
                     premium=(10.0 + i) * size * 100, gamma_used=gamma,
                     dealer_gamma_delta=delta)


# ── the bound (§15.6) ────────────────────────────────────────────────
def test_ring_buffer_cannot_grow_past_its_cap():
    b = TapeBuffer(cap=50)
    for i in range(5000):
        b.add(mk(i))
    assert b.stats()["buffered"] == 50
    assert b.stats()["prints_seen"] == 5000, "counters keep counting past the ring"


@pytest.mark.parametrize("batches,per", [(3, 1200), (1, 5000), (20, 50)])
def test_stored_ring_is_trimmed_by_rank_not_by_seq(tmp_path: Path, batches, per):
    """seq is a millisecond timestamp, not a dense counter — `max(seq) - cap`
    trims 2000 MILLISECONDS of prints, whatever number that happens to be."""
    store = StateStore(tmp_path / "s.duckdb")
    for b in range(batches):
        store.write_tape([mk(b * per + i) for i in range(per)], "I:SPX")
    with duckdb.connect(str(tmp_path / "s.duckdb"), read_only=True) as con:
        n = con.execute("SELECT count(*) FROM tape_print").fetchone()[0]
        newest = con.execute("SELECT max(price) FROM tape_print").fetchone()[0]
    assert n == min(batches * per, TAPE_CAP)
    assert newest == pytest.approx(10.0 + batches * per - 1), "newest kept"


def test_prints_are_partitioned_by_instrument(tmp_path: Path):
    store = StateStore(tmp_path / "s.duckdb")
    store.write_tape([mk(i) for i in range(10)], "I:SPX")
    store.write_tape([mk(i) for i in range(4)], "SPY")
    with duckdb.connect(str(tmp_path / "s.duckdb"), read_only=True) as con:
        assert dict(con.execute("SELECT underlying, count(*) FROM tape_print "
                                "GROUP BY 1").fetchall()) == {"I:SPX": 10, "SPY": 4}


# ── the diagnostics (§15.1) ──────────────────────────────────────────
def test_a_gamma_lookup_that_misses_everything_is_visible():
    """The failure this panel exists for: a key-shape mismatch returns 0 for
    every contract, the overlay goes flat, and it looks like a quiet market.
    Here it reads as a 100% miss rate."""
    led, tape = FlowLedger(), TapeBuffer()
    feed = OptionsFeed("k", ledger_for=lambda _r: led,
                       gamma_fn=lambda *a: 0.0,          # every lookup misses
                       spot_fn=lambda _r: 7657.0,
                       tape_for=lambda _r: tape)
    t = "O:SPXW260914C07650000"
    for px in (10.0, 10.5, 11.0, 11.5):
        feed._handle({"ev": "T", "sym": t, "p": px, "s": 10})

    st = tape.stats()
    assert st["gamma_miss_pct"] == pytest.approx(0.75), "3 classified, all missed"
    assert st["unclassified"] == 1, "the first print has no prior to compare to"
    assert led.net() == 0.0, "the map moved by nothing — which is the point"
    assert all(p.gamma_used == 0.0 for p in tape.recent() if p.side)


def test_dealer_gamma_delta_is_the_value_the_ledger_actually_wrote():
    """Recomputed in the UI it would be a second copy of the arithmetic, and a
    second place for the map and its own diagnostic to disagree."""
    led, tape = FlowLedger(), TapeBuffer()
    feed = OptionsFeed("k", ledger_for=lambda _r: led,
                       gamma_fn=lambda *a: GAMMA, spot_fn=lambda _r: 7657.0,
                       tape_for=lambda _r: tape)
    t = "O:SPXW260914C07650000"
    feed._handle({"ev": "T", "sym": t, "p": 10.0, "s": 7})
    feed._handle({"ev": "T", "sym": t, "p": 10.5, "s": 7})   # uptick -> buy

    applied = [p.dealer_gamma_delta for p in tape.recent() if p.side]
    assert applied and sum(applied) == pytest.approx(led.net())
    assert applied[0] < 0, "a customer buy leaves dealers shorter gamma"


def test_unclassified_prints_reach_the_tape_and_move_nothing():
    led, tape = FlowLedger(), TapeBuffer()
    feed = OptionsFeed("k", ledger_for=lambda _r: led,
                       gamma_fn=lambda *a: GAMMA, spot_fn=lambda _r: 7657.0,
                       tape_for=lambda _r: tape)
    feed._handle({"ev": "T", "sym": "O:SPXW260914P07500000", "p": 5.0, "s": 3})
    p, = tape.recent()
    assert p.side == 0 and p.gamma_used == 0.0 and p.dealer_gamma_delta == 0.0
    assert tape.stats()["unclassified"] == 1
    assert tape.stats()["gamma_misses"] == 0, "no side is not a lookup miss"


def test_drain_returns_each_print_once():
    b = TapeBuffer()
    for i in range(5):
        b.add(mk(i))
    assert len(b.drain()) == 5
    assert b.drain() == [], "a drained print is not published twice"


def test_reset_clears_the_session(tmp_path: Path):
    b = TapeBuffer()
    for i in range(10):
        b.add(mk(i))
    b.reset()
    s = b.stats()
    assert s["prints_seen"] == 0 and s["buffered"] == 0 and s["unclassified"] == 0


# ── what the store publishes (§15.3) ─────────────────────────────────
def test_stats_survive_the_ring_trimming_the_prints_they_counted(tmp_path: Path):
    """The honesty numbers live in their own table precisely so a long session
    does not lose them when the ring rolls over."""
    store = StateStore(tmp_path / "s.duckdb")
    store.write_tape([mk(i) for i in range(3000)], "I:SPX",
                     {"prints_seen": 3000, "contracts_seen": 15000,
                      "unclassified": 400, "gamma_misses": 900})
    with duckdb.connect(str(tmp_path / "s.duckdb"), read_only=True) as con:
        kept = con.execute("SELECT count(*) FROM tape_print").fetchone()[0]
        seen, uncl, miss = con.execute(
            "SELECT prints_seen, unclassified, gamma_misses FROM tape_stats"
        ).fetchone()
    assert kept == TAPE_CAP < seen
    assert (seen, uncl, miss) == (3000, 400, 900)


def test_right_column_is_not_a_reserved_word(tmp_path: Path):
    """`right` is RIGHT JOIN in DuckDB; the column is opt_right so no query
    has to quote it."""
    store = StateStore(tmp_path / "s.duckdb")
    assert store.available and store.write_tape([mk(0)], "I:SPX") == 1
    with duckdb.connect(str(tmp_path / "s.duckdb"), read_only=True) as con:
        cols = [r[1] for r in con.execute("PRAGMA table_info(tape_print)").fetchall()]
    assert "opt_right" in cols and "right" not in cols
