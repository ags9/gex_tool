# Round Five Pre-Registration — 15-Minute ORB to Prior-Day Extreme

**Supersedes nothing.** v1 and v2 governed rounds one to three (all failed;
v2 §7 closed the dealer-gamma entry family). v3 governs round four
(0DTE condors), which is independent and may be running concurrently.

This registers a **third family**: opening-range breakout with a prior-day
extreme as target. Commit and tag `prereg-v4` before any analysis.

Freeze date: ______________ · Tag: `prereg-v4` · Author: ags9

---

## 0. What this is, and what it deliberately is not

**The registered strategy is two levels and nothing else.** Price crosses
the 15-minute opening range in one direction; the prior day's extreme in
that direction has not yet been reached; enter; exit at that extreme.

Both levels are arithmetic. Neither requires interpretation. That is the
point: rounds one to three failed at exactly the place where a human read
("confirmed break", "rejection") had to be encoded, and the encoding turned
out not to be the thing the operator was doing.

**Deliberately excluded from this registration**, despite being proposed:

- *What to do when price has already crossed both the ORB and the prior
  extreme.* Proposed exit: a GEX level. Excluded because (a) the base case
  is untested, so there is nothing to compare a branch against, and (b)
  rounds one to three established that these levels do not predict
  direction. Using one as an exit target is a weaker claim than using it as
  an entry — "price tends to pause here" rather than "price tends to go
  here" — but it is the same family and needs its own registration, not a
  clause in this one. See §8.
- *Restricting the sample to 2024+.* See §2.
- Anything involving DEX, flow, regime, or the tape. All are computed and
  may be **recorded as context**; none may gate entry, exit, or sizing this
  round.

Each of these is individually reasonable. Together they are three
parameters and a branch on an untested base — which is how round one
happened, with the fitting occurring during specification rather than
tuning, where the sweep discipline cannot see it.

## 1. Hypotheses

**H1 (primary).** After price crosses the 15-minute opening range in one
direction, and with the prior session's extreme in that direction not yet
reached, price reaches that extreme more often than from matched-random
entries at the same times and directions.

**H2 (execution).** That is exploitable after realistic costs on near-ATM
SPX options.

**H3 (two claims, not one).** H1 is a *momentum* claim about the entry
(price continues past the opening range) and a *mean-reversion* claim about
the exit (price stalls at the prior extreme). Both may be true; they are
separable, and the report must say which is carrying the result. A strategy
whose entries are worthless but whose exits are well-placed is a different
finding from the reverse.

## 2. Sample window — the full development set, not 2024+

The operator proposed testing only 2024 onward, on the grounds that the
market became news-driven and the AI regime is structurally different.

**Registered decision: use the full window.** Reasons, recorded before any
result:

1. It halves the sample at the point where statistical power matters most.
2. "Recent data is more relevant" is the reasoning that produced round
   one's fitted result.
3. As stated it is unfalsifiable: if a strategy works post-2024 and fails
   before, nothing distinguishes "the market changed" from "we overfitted a
   smaller sample."

**The instinct is testable and is hereby registered as a claim**: if the
post-2024 market is structurally different for this strategy, per-era
results will differ across the boundary. The report gives per-year figures.
If they are indistinguishable, the premise was wrong and the full sample is
kept. If they differ sharply, that is a finding — and a *new* registration
about regime dependence, not a licence to drop the early years from this one.

| Partition | Dates | Use |
|---|---|---|
| Development | 2022-01-03 → 2025-12-31 | Unlimited exploration |
| Checkpoint | 2026-01-02 → freeze date | ONE run at the end |
| True holdout | Sessions after the freeze date | Shadow. The only clean test |

## 3. The registered strategy

### 3.1 Levels
- **Opening range:** high and low of ⚙09:30–09:45 ET, from SPX minute bars.
- **Prior-day extreme:** prior session's regular-hours high and low from
  `index_values`.
  **Registered as regular-hours only.** The operator notes he has used ES,
  whose near-24h session captures overnight moves the cash index misses.
  That is genuinely different information and may be better — but it needs
  a data source we do not have (futures entitlement unverified) and would
  be a different registered level. If ES or SPY-extended-hours ranges are
  later added, that is a new hypothesis.

### 3.2 Entry
All must hold:
1. Price crosses the opening range high (long) or low (short) — a ⚙1-minute
   close beyond it, not a touch.
2. The prior-day extreme in that direction has **not** been exceeded at any
   point in the session so far.
3. Distance from entry to that extreme is ≥ ⚙0.15% of spot (a target too
   close cannot pay the spread).
4. Time between ⚙09:45 and ⚙14:00 ET.
5. No position open. ⚙One entry per session per direction, max ⚙2 per day.

Instrument: SPX, ⚙1 DTE, nearest-ATM strike in the direction of the trade.

### 3.3 Exit
First match wins:
- **Target:** price reaches the prior-day extreme. ⚙2-point buffer so the
  exit does not depend on a tick.
- **Invalidation:** ⚙1-minute close back inside the opening range.
- **Time stop:** ⚙90 minutes.
- **Hard flat:** ⚙15:45 ET.
- Stop: ⚙−35% of premium. Registered as a candidate, not a default —
  round three found hard stops were the *worst* loss rule (−$107/trade vs
  −$87 for regime-flip), because stops pay an extra tick into wide NBBO
  spreads on cheap options. §6 measures all four exit rules on identical
  entries.

### 3.4 Sizing
⚙2.5% of tranche at risk per trade on a $25,000 tranche; max ⚙2 contracts.
Consistent with v3's amended sizing so the two rounds are comparable.

## 4. Method order (FROZEN)

1. Harness: ORB and prior-day extremes from stored minute bars; known-answer
   tests hand-computed to the cent, with mutation checks that bite, run under
   a bytecode-clearing harness bracketed by control runs on restored source.
2. **Control experiment first**, registered defaults, before any tuning.
3. Tuning only if Stage 1 passes. ≤⚙6 parameters total.
4. Control re-run after tuning.
5. Checkpoint. Once.
6. Shadow.

## 5. Null arms

- `random_time` — random entry minutes, real direction mix. Does crossing
  the ORB matter, or only being long/short on that day?
- `random_dir` — real entry times, random direction.
- `random_both` — baseline.
- `shuffled_levels` — real logic, **prior-day extremes donated from another
  session**. The sharpest test: same market, wrong target.
  **Trade counts must be matched by construction.** v2's shuffled arm traded
  95 times against 1,595 and its net was mostly volume (prereg defect P2).

## 6. What must be measured

- All four exit rules (§3.3) on **identical entries**. An exit rule that
  closes sooner must not change which entries occur.
- **Separation per H3:** hit rate of reaching the prior extreme, and the
  distribution of maximum favourable excursion. If price usually stalls
  short of the target, the exit is mispriced rather than the entry worthless.
- **Per-era figures** per §2.
- Credit/cost: spread paid and commission as a fraction of premium.
- Mark provenance. A bundle above ⚙25% model fallback is refused, not warned.

## 7. Criteria (FROZEN)

**Stage 1.** P(random ≥ strategy) ≤ 0.05 on all four arms, AND the
`shuffled_levels` arm must underperform on a **per-trade** basis, not net
(prereg defects P1/P2: against a losing strategy, an arm that trades less
flatters itself, and a net comparison inverts when net ≤ 0).

**Stage 2 — development set:**
- ≥300 IS trades, ≥100 OOS trades
- OOS profit factor ≥ 1.3
- OOS max drawdown ≤ 20% of tranche
- Annualised ≥ ⚙$5,000 at $25k
- PF excluding top 5 trades ≥ 1.15
- Loss distribution reported first; worst day ≤ ⚙5% of tranche

**Stage 3 — checkpoint.** PF ≥ 1.2, positive net, tail criteria hold.

**Stage 4 — shadow.** ≥20 sessions, ≥30 trades, signal parity clean.

## 8. Extensions, explicitly deferred

Registered here so they are not smuggled in as refinements:

1. **The both-crossed branch.** When price has already exceeded both the ORB
   and the prior extreme, exit at a GEX level instead. Requires its own
   registration and its own holdout, and may only be tested if this base
   case passes Stage 1.
2. **Overnight ranges** (ES or SPY extended hours) as the extreme.
3. **Regime, DEX, or flow gating.**

None may be added to this round. Adding any of them makes the result
uninterpretable, because the specification would then have been fitted to
the same data that judged it.

## 9. Kill criteria

Abandoned if Stage 1 fails, or Stage 2 needs more than ⚙6 parameters, or
Stage 3 fails after a Stage 2 pass.

This is the third strategy family tested. If it and round four both fail,
the honest conclusion is that the platform's value is observability and
exit management, and the search for an automated entry edge stops.
