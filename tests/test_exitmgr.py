"""Exit manager and shadow mode (docs/EXIT_MANAGER_SPEC.md).

The rules are small; the tests are mostly about the ones a distracted human
cannot enforce, and about the ladder — which the spec is explicit is a
recommendation with no backtest behind it, and which §3 exists to judge.
"""
import pytest

from gexbot.exitmgr import (ExitParams, ExitRule, ManagedPosition, Policy,
                            apply, evaluate)
from gexbot.shadow import ShadowBook, book_from_records

P = ExitParams()


def pos(**kw) -> ManagedPosition:
    d = dict(symbol="SPX", direction=1, contracts=3, entry_premium=10.0,
             entry_spot=7650.0, entry_minute=10 * 60, target_level=7700.0,
             next_level=7720.0)
    d.update(kw)
    return ManagedPosition(**d)


# ── §2.1 level target ────────────────────────────────────────────────
def test_target_needs_the_level_reached_not_approached():
    p = pos()
    assert evaluate(p, spot=7690.0, mark=14.0, minute=10 * 60 + 5) is None
    a = evaluate(p, spot=7699.0, mark=16.0, minute=10 * 60 + 5)   # inside buffer
    assert a and a.rule is ExitRule.LEVEL_TARGET


def test_a_short_position_reaches_its_target_downward():
    p = pos(direction=-1, target_level=7600.0, next_level=7580.0)
    assert evaluate(p, spot=7610.0, mark=14.0, minute=10 * 60 + 5) is None
    a = evaluate(p, spot=7601.0, mark=16.0, minute=10 * 60 + 5)
    assert a and a.rule is ExitRule.LEVEL_TARGET


def test_the_buffer_scales_with_the_instrument():
    """1.5 SPX points is 0.15 XSP points. Using raw points on XSP would make
    the buffer ten times too wide."""
    spx = pos(target_level=7700.0)
    xsp = pos(symbol="XSP", entry_spot=765.0, target_level=770.0,
              next_level=772.0, points_per_spx_point=0.1)
    assert evaluate(spx, spot=7699.0, mark=16.0, minute=10 * 60 + 1)  # 1 pt away
    assert evaluate(xsp, spot=769.9, mark=16.0, minute=10 * 60 + 1)   # 0.1 away
    assert evaluate(xsp, spot=769.0, mark=16.0, minute=10 * 60 + 1) is None


# ── §2.2 / §2.3 the rules a distracted human cannot enforce ──────────
def test_time_stop_fires_at_thirty_minutes():
    p = pos()
    assert evaluate(p, spot=7660.0, mark=11.0, minute=10 * 60 + 29) is None
    a = evaluate(p, spot=7660.0, mark=11.0, minute=10 * 60 + 30)
    assert a and a.rule is ExitRule.TIME_STOP and a.contracts == 3


def test_hard_flat_is_unconditional():
    p = pos(entry_minute=15 * 60 + 45)          # entered minutes before
    a = evaluate(p, spot=7660.0, mark=11.0, minute=P.hard_flat_minute)
    assert a and a.rule is ExitRule.HARD_FLAT and a.contracts == 3


def test_hard_flat_closes_a_runner_too():
    p = pos()
    apply(p, evaluate(p, spot=7700.0, mark=16.0, minute=10 * 60 + 5,
                      laddered=True))
    assert p.remaining == 1
    a = evaluate(p, spot=7710.0, mark=17.0, minute=P.hard_flat_minute,
                 laddered=True)
    assert a and a.rule is ExitRule.HARD_FLAT and a.contracts == 1


def test_backstop_is_wide_and_last():
    """It exists for a dead process, not as the risk management — option books
    gap, so it sits far away and the real exits fire first."""
    p = pos()
    assert evaluate(p, spot=7640.0, mark=5.1, minute=10 * 60 + 2) is None
    a = evaluate(p, spot=7640.0, mark=5.0, minute=10 * 60 + 2)
    assert a and a.rule is ExitRule.BACKSTOP


# ── §2.5 the ladder ──────────────────────────────────────────────────
@pytest.mark.parametrize("n,expected", [(1, (1, 0)), (2, (2, 0)), (3, (2, 1)),
                                        (4, (3, 1)), (6, (4, 2)), (10, (7, 3))])
def test_tranche_split_is_ceil_two_thirds(n, expected):
    assert pos(contracts=n).tranche_sizes(P) == expected


def test_one_contract_cannot_ladder():
    p = pos(contracts=1)
    a = evaluate(p, spot=7700.0, mark=16.0, minute=10 * 60 + 5, laddered=True)
    assert a.contracts == 1
    apply(p, a)
    assert p.remaining == 0 and not p.tranche_filled


def test_the_runner_stop_moves_to_entry_when_the_first_tranche_fills():
    p = pos()
    assert p.runner_stop_spot is None
    apply(p, evaluate(p, spot=7701.0, mark=16.0, minute=10 * 60 + 5,
                      laddered=True))
    assert p.tranche_filled and p.runner_stop_spot == p.entry_spot
    a = evaluate(p, spot=7649.0, mark=9.0, minute=10 * 60 + 9, laddered=True)
    assert a and a.rule is ExitRule.RUNNER_STOP


def test_the_time_stop_does_not_apply_to_a_runner():
    """A runner has already proven itself by reaching the target (§2.5)."""
    p = pos()
    apply(p, evaluate(p, spot=7701.0, mark=16.0, minute=10 * 60 + 5,
                      laddered=True))
    late = evaluate(p, spot=7705.0, mark=17.0, minute=10 * 60 + 45,
                    laddered=True)
    assert late is None, "the runner gets more rope"
    # ...but the un-laddered position would have been closed by now
    fresh = pos()
    assert evaluate(fresh, spot=7660.0, mark=11.0,
                    minute=10 * 60 + 45).rule is ExitRule.TIME_STOP


def test_the_runner_exits_at_the_next_level():
    p = pos()
    apply(p, evaluate(p, spot=7701.0, mark=16.0, minute=10 * 60 + 5,
                      laddered=True))
    a = evaluate(p, spot=7720.0, mark=19.0, minute=10 * 60 + 12, laddered=True)
    assert a and a.rule is ExitRule.RUNNER_NEXT_LEVEL and a.contracts == 1


# ── §3 three policies, from one position ─────────────────────────────
def _walk(book: ShadowBook, path):
    for minute, spot, mark in path:
        book.step(minute=minute, spot=spot, mark=mark, spread=0.40)


def test_both_policies_are_recorded_and_can_disagree():
    """The whole point of recording two: the ladder is a recommendation, and
    only both numbers from the same position can judge it."""
    book = ShadowBook(pos())
    _walk(book, [(605, 7690.0, 14.0), (608, 7701.0, 16.0),
                 (615, 7648.0, 9.0), (640, 7640.0, 8.0)])
    s = {x["policy"]: x for x in book.summaries()}
    assert set(s) == {"target_all", "ladder"}
    assert s["target_all"]["contracts_closed"] == 3
    assert s["ladder"]["contracts_closed"] == 3
    assert s["ladder"]["fills"] == 2 and s["target_all"]["fills"] == 1
    # on this path the runner gave back the extra — a real disagreement
    assert s["target_all"]["pnl"] != s["ladder"]["pnl"]


def test_the_ladder_pays_more_spread_because_it_crosses_twice():
    """§2.6. A ladder that wins on the P&L headline and loses on the spread
    has not won, so the two are recorded separately."""
    book = ShadowBook(pos())
    _walk(book, [(608, 7701.0, 16.0), (612, 7720.0, 19.0)])
    s = {x["policy"]: x for x in book.summaries()}
    assert s["ladder"]["fills"] == 2 and s["target_all"]["fills"] == 1
    assert s["ladder"]["spread_cost"] > s["target_all"]["spread_cost"]


def test_a_path_where_the_ladder_wins():
    """Deliberately the opposite case — the fixture must not only ever show
    the ladder losing, or the comparison proves nothing."""
    book = ShadowBook(pos())
    _walk(book, [(608, 7701.0, 16.0), (612, 7721.0, 24.0)])
    s = {x["policy"]: x for x in book.summaries()}
    assert s["ladder"]["pnl"] > s["target_all"]["pnl"]


def test_the_policies_do_not_share_state():
    book = ShadowBook(pos())
    _walk(book, [(608, 7701.0, 16.0)])
    assert book.runs[Policy.TARGET_ALL].position.remaining == 0
    assert book.runs[Policy.LADDER].position.remaining == 1
    assert not book.runs[Policy.TARGET_ALL].position.tranche_filled


def test_the_manual_exit_is_recorded_not_predicted():
    book = ShadowBook(pos())
    book.record_manual_exit(minute=612, spot=7680.0, mark=13.0, spread=0.40,
                            contracts=3)
    s = {x["policy"]: x for x in book.summaries()}
    assert s["manual"]["contracts_closed"] == 3
    assert "manual" in s and "target_all" in s and "ladder" in s, "all three"


def test_a_book_rebuilt_from_records_resumes_mid_ladder():
    """The engine restarts; positions outlive it. Replaying stored fills must
    restore the ladder's state, not resume it as though it were whole."""
    row = {"symbol": "SPX", "direction": 1, "contracts": 3,
           "entry_premium": 10.0, "entry_spot": 7650.0, "entry_minute": 600,
           "target_level": 7700.0, "next_level": 7720.0}
    stored = [{"shadow_id": 1, "policy": "target_all", "rule": "level_target",
               "contracts": 3},
              {"shadow_id": 2, "policy": "ladder", "rule": "level_target",
               "contracts": 2}]
    book = book_from_records(row, stored)
    assert book.runs[Policy.TARGET_ALL].position.remaining == 0
    ladder = book.runs[Policy.LADDER].position
    assert ladder.remaining == 1
    assert ladder.tranche_filled and ladder.runner_stop_spot == 7650.0
    # and it behaves like a runner, not like a fresh position
    a = evaluate(ladder, spot=7649.0, mark=9.0, minute=620, laddered=True)
    assert a and a.rule is ExitRule.RUNNER_STOP


def test_nothing_here_can_open_a_position():
    """Spec §6: it closes, it does not enter. There is no code path that adds
    contracts, and `apply` only ever reduces."""
    p = pos()
    before = p.remaining
    apply(p, evaluate(p, spot=7701.0, mark=16.0, minute=605, laddered=True))
    assert p.remaining < before
    assert not any(n.startswith("open") or n.startswith("enter")
                   for n in dir(p)), "no entry surface on a managed position"


# ── the comparison must be readable per instrument (§2.6, amended) ────
def test_shadow_comparison_carries_the_symbol(tmp_path):
    """Pooling SPX and XSP ladders would let SPX's tight book hide XSP's
    spread inside an average, so `symbol` travels with the comparison."""
    import datetime as dt

    from gexbot.api.reader import StateReader
    from gexbot.state import StateStore

    db = tmp_path / "s.duckdb"
    store = StateStore(db)

    def record(symbol: str, spread: float, mult: float) -> None:
        pid = store.open_paper_position(
            symbol=symbol, direction=1, contracts=3, strike=7650.0 * mult,
            expiry=dt.date(2026, 9, 16), entry_spot=7650.0 * mult,
            entry_premium=10.0 * mult, target_level=7700.0 * mult,
            next_level=7720.0 * mult)
        book = ShadowBook(pos(symbol=symbol, entry_premium=10.0 * mult,
                              entry_spot=7650.0 * mult,
                              target_level=7700.0 * mult,
                              next_level=7720.0 * mult,
                              points_per_spx_point=mult))
        for minute, spot, mark in [(608, 7701.0 * mult, 16.0 * mult),
                                   (612, 7721.0 * mult, 24.0 * mult)]:
            store.write_shadow_fills(
                pid, book.step(minute=minute, spot=spot, mark=mark,
                               spread=spread))

    record("SPX", 0.45, 1.0)      # tight book
    record("XSP", 5.86, 0.1)      # the spread measured live on 2026-09-12

    reader = StateReader(db)
    assert all("symbol" in r for r in reader.shadow_comparison())

    rollup = {(r["symbol"], r["policy"]): r for r in reader.shadow_by_symbol()}
    assert {s for s, _ in rollup} == {"SPX", "XSP"}
    # the mechanism the amendment describes: XSP's spread dwarfs SPX's on the
    # same path, which is why the two cannot be judged on a pooled average
    assert rollup[("XSP", "ladder")]["spread_cost"] > \
        rollup[("SPX", "ladder")]["spread_cost"] * 5
