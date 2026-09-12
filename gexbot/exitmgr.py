"""Exit manager — the human enters, the bot owns the exit (spec §0).

Pure logic, no I/O, in the same idiom as `exits.py` — and deliberately NOT in
`exits.py`. That module is Strategy C's exit brain, frozen under CLAUDE.md §3
and shared by the backtest and the live simulator; this is a different object
with a different job, and merging them would couple a paper tool to the code
whose signal-parity checks are the only forward test the project has.

What makes this buildable at all is what it refuses to do. It has no entry
logic, no directional view, and no opinion about what to trade. Autonomous
entry needs a validated edge and two rounds of testing failed to produce one
(CLAUDE.md §2). A rule that closes a position when the operator is in a
meeting needs no such proof.

v1 is paper only (spec §0.1). Nothing here places an order; it decides what
*would* close and at what price, and `shadow.py` records it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum


class ExitRule(str, Enum):
    LEVEL_TARGET = "level_target"        # §2.1
    TIME_STOP = "time_stop"              # §2.2
    HARD_FLAT = "hard_flat"              # §2.3
    BACKSTOP = "disaster_backstop"       # §2.4
    RUNNER_STOP = "runner_stop"          # §2.5 — breakeven stop after tranche 1
    RUNNER_NEXT_LEVEL = "runner_next_level"


class Policy(str, Enum):
    """The three outcomes recorded per position (spec §3).

    MANUAL is what the operator actually did. The other two are evaluated in
    parallel on the same position so the ladder can be judged against
    all-at-once from the record rather than from preference — the spec is
    explicit that the ladder is a recommendation, not an evidenced conclusion.
    """
    MANUAL = "manual"
    TARGET_ALL = "target_all"
    LADDER = "ladder"


@dataclass(frozen=True)
class ExitParams:
    """⚙ throughout. These are exit-management parameters for a paper tool —
    they are NOT strategy parameters and CLAUDE.md §3's tuning freeze does not
    reach them. They are also not tuned: they are the operator's stated
    behaviour written down."""
    time_stop_minutes: int = 30          # §2.2 "his winners work in ten"
    hard_flat_minute: int = 15 * 60 + 50 # §2.3 15:50 ET
    level_buffer_spx_pts: float = 1.5    # §2.1 "at or through, not approaching"
    backstop_premium_frac: float = 0.50  # §2.4 wide, by design
    ladder_target_fraction: float = 2 / 3


@dataclass
class ManagedPosition:
    """One paper position the bot is watching.

    `points_per_spx_point` carries the instrument's scale so a buffer stated
    in SPX points means the same distance on XSP, whose strikes are a tenth
    the size. Writing the buffer in raw points without it would make the XSP
    buffer ten times too wide.
    """
    symbol: str                          # "SPX" | "XSP"
    direction: int                       # +1 long call, -1 long put
    contracts: int
    entry_premium: float
    entry_spot: float
    entry_minute: int
    target_level: float
    next_level: float | None = None      # beyond the target, for the runner
    points_per_spx_point: float = 1.0    # SPX 1.0, XSP 0.1

    # mutable state
    remaining: int = field(init=False)
    tranche_filled: bool = False
    runner_stop_spot: float | None = None

    def __post_init__(self) -> None:
        self.remaining = self.contracts

    def tranche_sizes(self, p: ExitParams) -> tuple[int, int]:
        """(at target, runner). ⚙ ceil(n * 2/3) at target (spec §2.5).

        Weighted toward the target on purpose: the demonstrated edge is the
        quick move to the level, and continuation past it is speculation with
        no evidence behind it. One contract cannot ladder.
        """
        if self.contracts < 2:
            return (self.contracts, 0)
        at_target = math.ceil(self.contracts * p.ladder_target_fraction)
        return (at_target, self.contracts - at_target)


@dataclass
class ExitAction:
    rule: ExitRule
    contracts: int
    note: str


def _reached(spot: float, level: float, direction: int, buffer_pts: float) -> bool:
    """At or through the level, not merely approaching (spec §2.1).

    The buffer is applied on the near side, so a tick that grazes the level
    does not close the trade; price has to actually get there.
    """
    return (spot >= level - buffer_pts) if direction > 0 else (spot <= level + buffer_pts)


def evaluate(pos: ManagedPosition, *, spot: float, mark: float, minute: int,
             params: ExitParams | None = None,
             laddered: bool = False) -> ExitAction | None:
    """First match wins, in spec order: target, time stop, hard flat, backstop.

    Returns None to hold. Never mutates the position — the caller applies the
    action, so one position can be evaluated under several policies without
    them treading on each other.
    """
    p = params or ExitParams()
    if pos.remaining <= 0:
        return None
    buf = p.level_buffer_spx_pts * pos.points_per_spx_point
    at_target, runner = pos.tranche_sizes(p)
    is_runner_phase = laddered and pos.tranche_filled

    # §2.1 level target — the exit he would have taken anyway
    if not is_runner_phase and _reached(spot, pos.target_level, pos.direction, buf):
        n = at_target if (laddered and runner) else pos.remaining
        return ExitAction(ExitRule.LEVEL_TARGET, n,
                          f"spot {spot:,.2f} reached target {pos.target_level:,.2f}")

    # §2.5 runner, once the first tranche has filled
    if is_runner_phase:
        if pos.next_level is not None and _reached(spot, pos.next_level,
                                                   pos.direction, buf):
            return ExitAction(ExitRule.RUNNER_NEXT_LEVEL, pos.remaining,
                              f"runner reached next level {pos.next_level:,.2f}")
        # breakeven stop: once tranche 1 fills the trade cannot become a loser
        if pos.runner_stop_spot is not None:
            through = (spot <= pos.runner_stop_spot if pos.direction > 0
                       else spot >= pos.runner_stop_spot)
            if through:
                return ExitAction(ExitRule.RUNNER_STOP, pos.remaining,
                                  f"runner stopped at entry {pos.runner_stop_spot:,.2f}")

    # §2.2 time stop — the rule a distracted human cannot enforce. Applies to
    # the INITIAL position only: a runner has already proven itself.
    if not is_runner_phase and minute - pos.entry_minute >= p.time_stop_minutes:
        return ExitAction(ExitRule.TIME_STOP, pos.remaining,
                          f"{minute - pos.entry_minute} min without reaching target")

    # §2.3 hard flat — unconditional, runner included
    if minute >= p.hard_flat_minute:
        return ExitAction(ExitRule.HARD_FLAT, pos.remaining,
                          f"{p.hard_flat_minute // 60:02d}:"
                          f"{p.hard_flat_minute % 60:02d} unconditional flat")

    # §2.4 disaster backstop. Simulated here; in production it rests at the
    # broker for the case where this process, the Mac, or the link is dead —
    # it is not the risk management, because option books gap.
    if pos.entry_premium > 0:
        loss = (mark - pos.entry_premium) / pos.entry_premium
        if loss <= -p.backstop_premium_frac:
            return ExitAction(ExitRule.BACKSTOP, pos.remaining,
                              f"premium {loss:+.0%} — backstop would have filled")
    return None


def apply(pos: ManagedPosition, action: ExitAction) -> None:
    """Mutate the position for an action the caller has decided to take."""
    pos.remaining = max(0, pos.remaining - action.contracts)
    if action.rule is ExitRule.LEVEL_TARGET and pos.remaining > 0:
        # §2.5: the stop moves to entry the moment the first tranche fills
        pos.tranche_filled = True
        pos.runner_stop_spot = pos.entry_spot
