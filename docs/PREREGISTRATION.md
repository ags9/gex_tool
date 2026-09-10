# gexbot — Round Two Pre-Registration
**Status: DRAFT — must be committed to git and tagged BEFORE any round-two
analysis is run. Any change after the freeze date invalidates the holdout.**

Freeze date: ______________  ·  Git tag: `prereg-v1`  ·  Author: ags9

---

## 0. Why this document exists

Round one produced a strategy that beat four null models at p<0.05 on
2023–2026 and had **no edge whatsoever** on 2022 (random entries beat it
50–95% of the time). The parameters had been fitted on 2023–2026, so that
control result was partly guaranteed. We discovered a false positive only
because one period had never been touched.

This document exists to make the same mistake impossible twice: every
choice that could be bent to fit the data is written down here first.

## 1. Data partition (FROZEN — no exceptions)

| Partition | Dates | Use |
|---|---|---|
| **Development** | 2022-01-03 → 2025-12-31 | Unlimited exploration, sweeps, iteration |
| **Checkpoint** | 2026-01-02 → freeze date | ONE run, at the end. Weak evidence (already partly seen in round one) |
| **True holdout** | Every session after the freeze date | Paper trading. This is the real out-of-sample test |

Rules:
- No result from the checkpoint or holdout may cause a parameter change.
  If it does, the strategy re-enters development and needs a NEW holdout.
- Data that does not exist yet cannot be overfitted. The forward market is
  the only genuinely clean test available, so paper trading is not merely
  operational validation — it is the experiment.
- 2021 is excluded entirely (quote coverage too thin; 57% model marks).

## 2. Method order (FROZEN)

Round one ran 15 parameter sweeps before ever asking whether the entry beat
chance. That is backwards. Round two runs:

1. **Signal test first.** Control experiment (4 null arms, ≥20 seeds) on the
   development set, using DEFAULT parameters — not tuned ones.
2. **Only if signal is established** (§4 criteria) does any tuning happen.
3. **Control experiment repeated** after tuning, to confirm the edge is not
   an artifact of the tuning.
4. Checkpoint run. Once.
5. Paper trade.

## 3. Hypotheses (stated before analysis)

**H1 (primary).** Dealer gamma levels computed from classified intraday
options flow carry information about SPX price behaviour that is not
present in randomly-selected entries at matched times and directions.
- Test: `shuffled_levels` arm must underperform the strategy. This is the
  sharpest test — same logic, same market, wrong walls.

**H2.** The information in H1 is exploitable after realistic costs
($0.65/contract, half-spread each side, tick slippage on stops).

**H3 (regime dependence — the round-one puzzle).** If H1 holds on some
sub-periods and not others, the gating condition must be specified
*in advance* and tested, not discovered post hoc. Candidate gates to
pre-specify: VIX regime, 0DTE share of volume, average option spread
width, realised-vs-implied vol spread.

**Explicitly NOT hypotheses** (things round one assumed without testing):
- That mean-reversion at walls works (bounce book lost money: −$5,082).
- That momentum at walls works (breakout book: in-sample only).
- That the C.5 exit machinery adds value (sweeps showed exits were inert).

## 4. Success criteria (FROZEN — these numbers do not move)

**Stage 1 — signal:** on the development set with default parameters,
P(random ≥ strategy) ≤ 0.05 on ALL FOUR null arms, AND `shuffled_levels`
median ≤ 25% of strategy net. If this fails, the hypothesis is rejected
and no tuning is permitted.

**Stage 2 — after tuning:** all §10 gates green on the development set:
- ≥300 IS trades, ≥100 OOS trades (walk-forward within development)
- OOS profit factor ≥ 1.3
- OOS max drawdown ≤ 20% *(raised from 12%: momentum strategies carry
  deeper drawdowns; justified here, in advance, not after seeing results)*
- Annualised P&L clears $3,600 at the intended tranche
- PF excluding top 5 trades ≥ 1.15
- Control experiment re-passes at p ≤ 0.05

**Stage 3 — checkpoint (2026):** profit factor ≥ 1.2 and positive net.
A miss here means back to development WITH A NEW HOLDOUT — not a tweak.

**Stage 4 — paper trading (the real test):** ≥20 sessions, ≥30 trades,
signal parity clean, and P&L direction consistent with development
expectations. Live money requires all four stages.

## 5. Anti-fooling rules

- **Log every run.** `docs/RUNLOG.md` gets one line per backtest: date,
  command, purpose, result. Retrospective narrative is not permitted.
- **Label every run** as `development` or `evidence` when launched. A run
  whose parameters change afterwards was development, always.
- **Parameter count budget.** No more than ⚙6 tuned parameters total. Each
  additional degree of freedom fitted to the same data raises the false-
  positive rate; round one fitted at least 5 and still failed out-of-sample.
- **Prefer plateaus, refuse boundaries.** A value at the edge of its swept
  grid is not adopted — extend the grid or leave the parameter at default.
  (Round one's sweeps returned edge-of-grid "plateaus" three times, which
  actually meant the exits had become inert.)
- **Inert parameters get removed, not adopted.** If a parameter's value
  makes no difference across its range, delete the mechanism.

## 6. Kill criteria (agreed in advance)

The project stops — or the strategy family is abandoned — if:
- Stage 1 fails on the development set, OR
- Stage 2 requires more than ⚙6 tuned parameters to pass, OR
- Stage 3 fails after a Stage-2 pass, twice with different approaches, OR
- Paper trading shows signal parity failures that cannot be explained.

Stopping is a successful outcome. The instrument works; the cost of a
negative result is a few hundred dollars of data and some weeks. The cost
of ignoring one is the tranche.

## 7. What round one leaves us (assets, not liabilities)

- A pipeline that ingests 7-billion-row days and survives.
- Ledgers, greeks, entry/exit/discipline engines, all tested (32 tests).
- Honest marks (REST NBBO, provenance-tracked, ~0% fallback in-window).
- A backtest runner with era splits and executable gates.
- A control-experiment harness with four null models.
- Parity reconstruction for pre-2023 spot.
- Discord alerting; a results explorer UI.

None of this is invalidated by the negative result. It is the apparatus
that produced the negative result — which is the point.

---

**Signature line.** By committing this file, the author agrees that no
parameter, gate threshold, or partition boundary above will be changed
except by writing a NEW pre-registration document that supersedes this one
and starts a fresh holdout.
