# Round Three Pre-Registration — Range Mean Reversion

**Supersedes nothing.** `PREREGISTRATION.md` (tag `prereg-v1`) governed
rounds one and two and its kill criteria fired. This document registers a
**new hypothesis** and starts a **new holdout**. It must be committed and
tagged `prereg-v2` before any analysis is run.

Freeze date: ______________ · Tag: `prereg-v2` · Author: ags9

---

## 0. Why a third attempt is defensible

Two hypotheses from this family have already failed:

| Round | Hypothesis | Result |
|---|---|---|
| 1 | Enter on a confirmed **break through** a wall in negative gamma | Strong in-sample 2023–26; **no edge** on 2022 (random beat it 50–95%) |
| 2 | Same, pre-registered, control-first on 2022–25 | **Failed Stage 1.** Full strategy lost to random on every arm |

This is a third attempt with the same data, and the base rate is not
encouraging. It is registered anyway for one reason: **rounds one and two
tested a strategy the operator does not trade.** They tested momentum
*through* levels on a multi-hour horizon. He trades mean reversion *toward*
a magnet, entering before a boundary, on a horizon of minutes to hours.
Those are near-opposites, not variants.

If this fails, the family is done. See §7.

---

## 1. The hypothesis, stated before any analysis

**H1 (primary).** When spot approaches a range boundary derived from the
dealer-gamma profile, price subsequently moves toward the magnet (the
largest positive-gamma strike inside the range) more often, and further,
than from matched-random entries at the same times and directions.

**H2 (execution).** That movement is exploitable after realistic costs on
near-ATM options at ⚙3 DTE.

**H3 (regime dependence — pre-specified, not discovered).** H1 holds in
positive-gamma regimes and fails or reverses in negative-gamma regimes. The
gate is the engine's own regime call with its existing dead zone. If H1
holds only under some *other* condition discovered after looking, that is a
new hypothesis requiring its own registration — not a refinement of this one.

**Explicitly not hypotheses here:**
- That holding past the magnet when gamma is stacked beyond it adds value
  (§3.3 measures it; it is not assumed).
- That waiting for a regime flip is a good loss rule (§3.2 measures it
  against alternatives; the operator's current practice is one candidate,
  not the default).
- Anything about entry *timing* within the approach — "rejection", wicks,
  failed touches. Deliberately excluded: rounds one and two both failed at
  exactly the point where a human read had to be encoded, and the operator
  has since clarified the rejection was confirmation for comfort, not the
  trigger.

## 2. Data partition (FROZEN)

| Partition | Dates | Use |
|---|---|---|
| Development | 2022-01-03 → 2025-12-31 | Unlimited exploration |
| Checkpoint | 2026-01-02 → freeze date | ONE run at the end; weak evidence (already seen in prior rounds) |
| True holdout | Sessions after the freeze date | Paper/shadow trading. The only clean test |

2021 excluded (quote coverage too thin; 57% model marks).

**Bar resolution changes.** The replay currently works in 5-minute bars.
These trades resolve in minutes, so minute bars are required. This is a
harness change, not a strategy change, and must land before any run.

## 3. What gets measured

### 3.1 Entry
Spot within ⚙0.075% of a range boundary (≈5-6 SPX points at current levels),
moving toward it, in a positive-gamma regime. Direction: toward the magnet.
Proportional rather than a fixed point count so it scales across price
levels.

Boundary and magnet come from the engine's own profile. No external feed.

### 3.2 Loss rule — three candidates, measured against identical entries
1. **Regime flip** (the operator's current practice): hold until the engine
   calls a regime change, then exit.
2. **Hard stop** at ⚙-30% of premium.
3. **Time stop** at ⚙60 minutes.

The concern being tested: waiting for a regime flip is slow. Price can
travel far through a boundary before net gamma crosses zero, and on a 3 DTE
option that is a large loss by the time the signal arrives. A high win rate
with occasional −60% trades can still have negative expectancy. **Report the
full loss distribution, not the win rate**, for each rule.

### 3.3 Target — two candidates
1. **Magnet** — close on reaching it.
2. **Magnet plus continuation** — hold past it when gamma is stacked
   beyond, with a stop at the magnet.

Whether continuation is worth anything is an open question, not an
assumption.

### 3.4 DTE
⚙3 DTE default, reflecting the operator's stated reason: price can linger at
a level for extended periods and 0 DTE theta punishes waiting. Test 1, 2, 3
and 5 DTE. **Note:** a short time stop would cut precisely the trades the
DTE choice exists to accommodate — so §3.2's time-stop candidate and the DTE
ladder interact and must be read together.

## 4. Method order (FROZEN)

1. **Minute-bar harness** built and verified against known days.
2. **Control experiment first**, default parameters, development set, four
   null arms ≥20 seeds. No tuning before this.
3. Tuning only if Stage 1 passes, ≤⚙6 parameters total.
4. **Control re-run** after tuning.
5. Checkpoint. Once.
6. Shadow/paper on live sessions.

## 5. Criteria (FROZEN — these numbers do not move)

**Stage 1 — signal.** P(random ≥ strategy) ≤ 0.05 on **all four** null arms,
AND `shuffled_levels` median ≤ 25% of strategy net. Failure here ends it.

**Stage 2 — after tuning, development set:**
- ≥300 IS trades, ≥100 OOS trades (walk-forward within development)
- OOS profit factor ≥ 1.3
- OOS max drawdown ≤ 20%
- Annualised P&L clears $3,600 at the intended tranche
- PF excluding top 5 trades ≥ 1.15
- Control re-passes at p ≤ 0.05
- **Loss distribution:** worst single trade ≤ ⚙40% of premium under the
  selected loss rule; no single day worse than ⚙-15% of tranche

**Stage 3 — checkpoint (2026).** PF ≥ 1.2 and positive net. A miss sends it
back to development **with a new holdout**, not a tweak.

**Stage 4 — shadow.** ≥20 sessions, ≥30 trades, signal parity clean.

## 6. Anti-fooling rules

Carried from `prereg-v1` §5, unchanged: run log with every run labelled
`development` or `evidence` when launched; ≤6 tuned parameters; refuse
edge-of-grid values; delete inert mechanisms rather than adopting them.

Added for this round:

- **Win rate is not a headline.** Every report leads with the loss
  distribution. A strategy whose losses are deferred until a slow signal
  fires will show a flattering win rate; that is the specific failure mode
  under test.
- **The three loss rules and two targets are measured on identical
  entries.** No entry logic changes between them.

## 7. Kill criteria

The strategy family is abandoned — not retried — if:

- Stage 1 fails on the development set, OR
- Stage 2 needs more than ⚙6 tuned parameters, OR
- Stage 3 fails after a Stage 2 pass.

This is the third hypothesis tested against this data. A fourth would be
fishing. If this fails, the honest conclusion is that the platform's value
is observability and exit management, not autonomous entry, and the project
stops looking for an entry edge in dealer-gamma levels.

---

**Signature.** Committing this file means no parameter, criterion, or
partition above changes except by a new pre-registration that supersedes it
and starts a fresh holdout.
