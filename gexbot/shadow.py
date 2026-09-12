"""Shadow mode — record what the bot would have done (spec §3).

Before the bot is allowed to close anything it runs in shadow: the operator
enters and exits by hand as he does today, and this records the
counterfactual. Three outcomes per position, never two —

  1. what the operator actually did,
  2. what "close everything at the target" would have produced,
  3. what the §2.5 ladder would have produced.

The third is the whole point of recording the second. The ladder is a
recommendation with no backtest behind it, and the only way to find out
whether it is an improvement or a preference is to have both numbers from the
same positions. Recording one and not the other would be unrecoverable later.

Spreads are counted per tranche (§2.6). Scaling out crosses the book twice,
and on XSP that is not a rounding error — a ladder that wins on the P&L
headline and loses on the spread has not won.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .costs import CostParams, FillModel
from .exitmgr import ExitParams, ExitRule, ManagedPosition, Policy, apply, evaluate


@dataclass
class Fill:
    """One simulated tranche close. Paper only — nothing was sent anywhere."""
    policy: Policy
    rule: ExitRule
    minute: int
    spot: float
    mark: float
    fill_price: float
    contracts: int
    spread_cost: float
    commission: float
    pnl: float

    @property
    def net_pnl(self) -> float:
        return self.pnl


@dataclass
class PolicyRun:
    """One position evaluated under one policy.

    Holds its own copy of the position so the policies cannot tread on each
    other's state — the ladder mutates `tranche_filled` and the breakeven
    stop, and target-all must not see either.
    """
    policy: Policy
    position: ManagedPosition
    laddered: bool
    params: ExitParams = field(default_factory=ExitParams)
    costs: CostParams = field(default_factory=CostParams)
    fills: list[Fill] = field(default_factory=list)

    def step(self, *, minute: int, spot: float, mark: float,
             spread: float) -> Fill | None:
        """Advance one tick. Returns a Fill if a rule fired."""
        if self.position.remaining <= 0:
            return None
        action = evaluate(self.position, spot=spot, mark=mark, minute=minute,
                          params=self.params, laddered=self.laddered)
        if action is None:
            return None

        fm = FillModel(self.costs)
        # A backstop is a resting stop, so it pays the extra adverse tick the
        # fill model applies to stop-triggered exits. The others are worked
        # against live quotes.
        stop = action.rule in (ExitRule.BACKSTOP, ExitRule.RUNNER_STOP)
        px, _drag = fm.sell(mark, spread, action.contracts, stop_triggered=stop)

        spread_cost = (mark - px) * self.costs.multiplier * action.contracts
        commission = self.costs.commission_per_contract * action.contracts
        pnl = ((px - self.position.entry_premium)
               * self.costs.multiplier * action.contracts) - commission

        fill = Fill(self.policy, action.rule, minute, spot, mark, px,
                    action.contracts, spread_cost, commission, pnl)
        self.fills.append(fill)
        apply(self.position, action)
        return fill

    @property
    def closed(self) -> bool:
        return self.position.remaining <= 0

    def summary(self) -> dict:
        return {
            "policy": self.policy.value,
            "fills": len(self.fills),
            "contracts_closed": sum(f.contracts for f in self.fills),
            "pnl": sum(f.pnl for f in self.fills),
            "spread_cost": sum(f.spread_cost for f in self.fills),
            "commission": sum(f.commission for f in self.fills),
            "rules": [f.rule.value for f in self.fills],
            "open_contracts": self.position.remaining,
        }


def book_from_records(position: dict, fills: list[dict], *,
                      params: ExitParams | None = None,
                      costs: CostParams | None = None) -> "ShadowBook":
    """Rebuild a book from what is stored, rather than from process memory.

    The engine restarts; positions outlive it. Replaying the recorded fills
    restores each policy's state — remaining contracts, whether the first
    tranche filled, where the runner's breakeven stop sits — so a restart
    cannot silently resume a ladder as though it were still whole. Spec §7
    asks for the same discipline against broker state; this is its paper
    equivalent.
    """
    pos = ManagedPosition(
        symbol=position["symbol"], direction=int(position["direction"]),
        contracts=int(position["contracts"]),
        entry_premium=float(position["entry_premium"]),
        entry_spot=float(position["entry_spot"]),
        entry_minute=int(position["entry_minute"]),
        target_level=float(position["target_level"]),
        next_level=position.get("next_level"),
        points_per_spx_point=0.1 if position["symbol"] == "XSP" else 1.0)
    book = ShadowBook(pos, params=params or ExitParams(),
                      costs=costs or CostParams())
    for row in sorted(fills, key=lambda r: r.get("shadow_id", 0)):
        policy = Policy(row["policy"])
        if policy is Policy.MANUAL:
            continue
        run = book.runs[policy]
        action_rule = ExitRule(row["rule"])
        apply(run.position, _Replay(action_rule, int(row["contracts"])))
    return book


@dataclass
class _Replay:
    """Minimal stand-in so `apply` can replay a stored fill."""
    rule: ExitRule
    contracts: int


def _clone(pos: ManagedPosition) -> ManagedPosition:
    return ManagedPosition(
        symbol=pos.symbol, direction=pos.direction, contracts=pos.contracts,
        entry_premium=pos.entry_premium, entry_spot=pos.entry_spot,
        entry_minute=pos.entry_minute, target_level=pos.target_level,
        next_level=pos.next_level,
        points_per_spx_point=pos.points_per_spx_point)


@dataclass
class ShadowBook:
    """Both bot policies for one position, stepped together.

    The operator's own exit is recorded separately when it happens — the bot
    never sees it coming and must not be able to react to it (spec §6: if the
    operator closes it himself, the bot stands down).
    """
    position: ManagedPosition
    params: ExitParams = field(default_factory=ExitParams)
    costs: CostParams = field(default_factory=CostParams)
    runs: dict[Policy, PolicyRun] = field(init=False)
    manual: list[Fill] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.runs = {
            Policy.TARGET_ALL: PolicyRun(Policy.TARGET_ALL, _clone(self.position),
                                         laddered=False, params=self.params,
                                         costs=self.costs),
            Policy.LADDER: PolicyRun(Policy.LADDER, _clone(self.position),
                                     laddered=True, params=self.params,
                                     costs=self.costs),
        }

    def step(self, *, minute: int, spot: float, mark: float,
             spread: float) -> list[Fill]:
        out = []
        for run in self.runs.values():
            f = run.step(minute=minute, spot=spot, mark=mark, spread=spread)
            if f is not None:
                out.append(f)
        return out

    def record_manual_exit(self, *, minute: int, spot: float, mark: float,
                           spread: float, contracts: int) -> Fill:
        """What the operator actually did. Recorded, never predicted."""
        fm = FillModel(self.costs)
        px, _ = fm.sell(mark, spread, contracts)
        spread_cost = (mark - px) * self.costs.multiplier * contracts
        commission = self.costs.commission_per_contract * contracts
        pnl = ((px - self.position.entry_premium)
               * self.costs.multiplier * contracts) - commission
        fill = Fill(Policy.MANUAL, ExitRule.LEVEL_TARGET, minute, spot, mark,
                    px, contracts, spread_cost, commission, pnl)
        self.manual.append(fill)
        return fill

    @property
    def all_closed(self) -> bool:
        return all(r.closed for r in self.runs.values())

    def summaries(self) -> list[dict]:
        out = [r.summary() for r in self.runs.values()]
        if self.manual:
            out.append({
                "policy": Policy.MANUAL.value,
                "fills": len(self.manual),
                "contracts_closed": sum(f.contracts for f in self.manual),
                "pnl": sum(f.pnl for f in self.manual),
                "spread_cost": sum(f.spread_cost for f in self.manual),
                "commission": sum(f.commission for f in self.manual),
                "rules": ["manual"] * len(self.manual),
                "open_contracts": 0,
            })
        return out
