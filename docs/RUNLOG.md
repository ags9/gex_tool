# Run log

Mandatory under `PREREGISTRATION.md` §5 and `PREREGISTRATION_V2.md` §6: every
backtest, control and sweep run is recorded here **at launch**, labelled
`development` or `evidence`, with the threshold and the bar resolution that
judged it. Retrospective narrative is not permitted — a run whose parameters
changed afterwards was `development`, always.

Columns: date · label · command · purpose · result.

---

## 2026-09-12 — harness work, no strategy runs

| date | label | command | purpose | result |
|---|---|---|---|---|
| 2026-09-12 | `development` | *(no strategy command)* | Build minute-bar replay per `PREREGISTRATION_V2.md` §2/§4.1. Harness only — no entry, exit or discipline logic touched. | Landed. `ReplayBuilder(bar_minutes=…)`, default 5. 5-minute path verified bit-identical to `d7c8fe2` on 10 real days (bars) and 61 real days (full simulator, 41 trade legs, net −$516.02 both). Minute bars verified to reconstruct the same session high/low/close as the 5-minute path on 9 real days. 201 tests pass. |

| 2026-09-12 | `development` | `daysim` over `2024-01-02 → 2024-03-29`, Strategy C defaults, offline marks, `bar_minutes=5` then `1` | Harness smoke test: does minute replay run end to end on real days, and how far apart are the two resolutions? | Ran. 61 days both. **5-min:** 78 bars/day, 41 trade legs, net **−$516.02** — identical to `d7c8fe2` trade-for-trade. **1-min:** 390 bars/day, 52 trade legs, net **−$1,513.76**. |

**The 1-minute figure above is not evidence about anything.** It is Strategy C —
the rounds one and two breakout/bounce hypothesis whose kill criteria already
fired — run on a 61-day slice of the development set to prove the harness
works. More entry evaluation points produce more signals; both numbers are
negative, which is consistent with the two prior failures and tests nothing new.
The round-three hypothesis (`PREREGISTRATION_V2.md` §3) has no implementation
yet.

---

## 2026-09-12 — Stage 1 attempt, round three · `informative` (DOWNGRADED)

**Logged at launch, before any result existed. Downgraded from `evidence` to
`informative` at 2026-09-12, also before any result was read — see the amendment
immediately below.**

| field | value |
|---|---|
| label | **`informative`** — downgraded from `evidence` before the result was read (see amendment) |
| command | `python -m gexbot control --start 2022-01-03 --end 2025-12-31 --seeds 20 --bar-minutes 1 --strategy range --loss-rule regime_flip --target magnet --dte 3` |
| purpose | `PREREGISTRATION_V2.md` §4.2 Stage 1 — does the §3.1 boundary→magnet entry beat matched-random entries? |
| partition | Development, 2022-01-03 → 2025-12-31 (§2). Checkpoint and holdout untouched. |
| parameters | **DEFAULT** `RangeParams()`: boundary proximity 0.075%, hard stop −30%, time stop 60 min, 3 DTE. Nothing tuned; §4.3 forbids tuning before this passes. |
| resolution | 1-minute bars (§2) |
| seeds | 20 per arm, 4 null arms (§4.2) |
| `GEX_BREAKOUT_ONLY` | unset — irrelevant to the range strategy, which does not read it |
| judged against | §5 Stage 1: P(random ≥ strategy) ≤ 0.05 on **all four** arms, AND `shuffled_levels` median ≤ 25% of strategy net |

**Primary arm designated in advance: `regime_flip` + `magnet`.** §3.2 candidate 1
is "the operator's current practice" and §3.3 candidate 1 is the plain magnet
target, so that pair is the registered default. The other five §3.2 × §3.3
combinations are run on the same build and reported as **descriptive only**.
Stage 1's verdict rests on the primary arm alone — six sets of p-values with the
best one promoted afterwards would be the round-one error in a new costume.

### Sample limitation that travels with this result

**95 of the 974 replayable development sessions (9.8%) end at or before 15:00
ET, not 16:00.** Measured on this run's own sample; an earlier estimate from the
index-file survey said 85 of 996 (8.5%), and the difference is the counting rule
(that survey keyed on a session's last index print at exactly 14:59, this counts
any session whose last BAR closes at or before 15:00, which also catches the
handful of early-close half-days). The sample figure is the one to quote. The
upstream index flat files are capped at 19:59 UTC, which is 15:59 ET in EDT and
14:59 ET in EST; the affected days are Jan–mid-Mar and Nov–Dec 2022. Those
sessions end before the 15:50 EOD flat, so a position still open at 14:59 is
closed by the bars running out rather than by any rule. Not repairable from
here: the REST aggregates endpoint returns no 2022 index data (measured) and raw
flat files are deleted after conversion per spec §18.

**Also true of ~85% of days:** minute bars carry no wick information, because
`index_rest.fetch_index_day` stores only each minute's close. High and low
degenerate to max/min(open, close) on those days. 2023's 165 flat-file days have
real sub-minute prints.

### Relaunched once, before any result was read

The first launch was stopped 7 minutes in (build stage, no results produced) and
restarted on corrected code. Recorded because the prereg's value is a clean
record, and "restarted the evidence run" is exactly the kind of thing that must
not be quietly omitted:

**§3.3's "gamma stacked beyond the magnet" was vacuously true.** The first
implementation asked whether a WALL lay beyond the magnet — but `ledger.levels`
picks the magnet strictly BETWEEN the two walls, so the far wall is always
beyond it. The condition returned True in 54 of 54 constructed cases, which
silently turned §3.3 candidate 2 into "always hold past the magnet": a
conditional rule with no condition. It is now measured as "at least as much
positive dealer gamma beyond the magnet as the magnet itself carries", a
parameter-free reading of a clause the prereg registers without defining a
threshold. `ledger.levels()` gained `magnet_gex`, `pos_gex_above_magnet` and
`pos_gex_below_magnet` to support it, and the level tuple is now 7 slots.

The **primary arm was unaffected either way** — `TargetRule.MAGNET` never reads
that flag — so the restart changed nothing about the Stage 1 verdict. It was
restarted so the six-arm §3.2 × §3.3 comparison in the same bundle would be
valid rather than carrying three footnoted columns. The incidence of the
precondition is now measured and reported in `run_meta.json`, so the
continuation arms can be read knowing how often the condition actually held.

Also fixed before relaunch: `control._forced_entry_day_range` still unpacked 4
level slots and raised on 7. **229 tests were green while that was broken** —
the control's range path had no coverage. It has a test now.

### Relaunched a second time — regime confirmation was a bar count

Also stopped in the build stage, no results read. Found while reviewing the
harness against the original brief, which named "the confirmation logic" as
something to check rather than assume — and this was the one I had missed:

**The regime classifier confirmed a regime change in BARS, not minutes.** A
candidate regime had to persist 2 bars to be adopted — 10 minutes at 5-minute
bars, **2 minutes** at 1-minute bars. So the regime flickered five times more
readily at the resolution this round runs at, which matters twice over:
prereg-v2 gates every entry on `regime == "P"` (H3), and its **primary** §3.2
loss rule exits on a regime flip whose registered concern is precisely that it
is SLOW. Left unfixed, the run would have measured a fast regime and reported it
against a criterion written for a slow one.

Now held as `replay.REGIME_CONFIRM_MIN` (10 wall-clock minutes), and the state
machine is extracted as `replay.confirm_regimes()` so it can be tested directly
— buried in the bar loop, the only way to reach it was through a ledger fixture
that had to swing net GEX's sign on cue, and the test that could not do that
**passed whether the cadence was a duration or a bar count**.

**Second finding, from the extraction: the P/N asymmetry was inert.** The code
read `pend_n >= (1 if cand == "N" else 2)`, apparently confirming negative gamma
in one bar and positive in two. It cannot: a candidate is recorded with
`pend_n = 1` in the `else` branch and the threshold is only ever tested in the
`elif` branch on the following bar, when `pend_n` is already 2. Negative gamma
was never quicker to confirm. Removed rather than carried forward, per
CLAUDE.md §10, with a test asserting the two thresholds were equivalent — which
is why removing it left 5-minute behaviour untouched.

Strategy C re-verified bit-identical to `d7c8fe2` after this change: 61 days,
41 trade legs, net −$516.02, trade-for-trade.

### Interpretation choices §3 does not settle

Recorded before the result, so they cannot be reverse-fitted to it. Each is a
place the pre-registration registers an idea without pinning the mechanics:

1. **The target is the magnet as it stood AT ENTRY**, not re-read as the profile
   refreshes every 15 minutes. §3.3 says "close on reaching it" without saying
   which "it" when the magnet moves. Entry-fixed makes the trade's thesis
   falsifiable against the level that justified it. A tracking target is the
   defensible alternative and would be a different registered question.

2. **"Gamma stacked beyond" mixes two clocks.** The sum of positive gamma beyond
   the magnet is read from the CURRENT bar, while the yardstick it is compared
   against — the magnet's own gamma — is from the entry bar. Fully-current or
   fully-entry-fixed are both more consistent than this; it affects only the
   three descriptive `*_continuation` arms, never the primary, and it is left
   as-is rather than changed mid-run. **Worth fixing before any §3.3 conclusion
   is drawn.**

3. **Overlapping positions are each sized at 40% of the tranche independently.**
   §6's identical-entries requirement forced overlap (an earlier-exiting loss
   rule must not take entries the slower rules never saw), but the sizing was
   not re-derived for it, and peak concurrency is neither capped nor recorded.
   So **absolute net P&L is not a realistic tranche return** — it is a
   comparable quantity across arms, which is all Stage 1 needs, and it would
   have to be re-derived before Stage 2's "annualised P&L clears $3,600" or its
   "no single day worse than −15% of tranche" could mean anything. Peak
   concurrency measured 1 on a synthetic oscillating session, but that session
   is smooth and `regime_flip` can hold for hours on real data. **Measure it on
   the real sample before reading any dollar figure as a tranche return.**

4. **Does the dead zone count as a regime change?** §3.2 candidate 1 says "hold
   until the engine calls a regime change, then exit". The engine's third state
   `"X"` means **no information** — inside the |net GEX| dead zone — and is
   explicitly *not* a third regime (CLAUDE.md §5.17). So entering it is the
   engine *declining* to call a change, not calling one. This run uses the
   **strict** reading: any loss of `"P"` exits, including into `"X"`. The
   literal reading — exit only on a confirmed flip to `"N"` — would hold through
   dead-zone periods and make the rule slower still, which is §3.2's registered
   concern. **This is the PRIMARY arm's loss rule, so the choice matters to the
   headline.** Sensitivity to it is measured on a sample in
   `sample_context.json` rather than asserted.

   (The entry gate is not ambiguous in the same way: entering requires the
   engine to positively assert `"P"`, so `"X"` blocks entry. Requiring
   information to open and requiring its absence to close are different
   questions.)

### Relaunched a third time — positions were vanishing from the sample

Stopped 26 minutes in, build stage, no results read.

**A position still open when the bars ran out was DISCARDED, with no trade
record.** Not neutral: the positions that survive to the last bar are the
*unresolved* ones, so for a slow loss rule like `regime_flip` the disappearances
skew toward trades not yet stopped out — flattering precisely the loss
distribution §6 requires to be reported **in full**, and §6 exists because a
flattering picture of deferred losses is the specific failure mode under test.

**Measured on real sessions: 36% of positions vanished on truncated days (4 of
11); 0% on full days.** The mechanism is the 85 sessions that end at 14:59 while
the entry window runs to 14:30 and the EOD flat is 15:50 — those days can never
close a late position by rule. Now 0% on both, re-measured.

Such closes are recorded as `session_end_data`, deliberately **not** `eod_flat`:
one is a rule firing at 15:50, the other is the feed ending early, and a result
has to be able to say how much of its sample was closed by the strategy versus
by missing data. The null arms record them too — otherwise the arms would stop
having matched trade counts.

`daysim.py` (Strategy C) has the same hole. Left alone: that hypothesis is dead
under §7, and each further change to it makes the recorded rounds one/two
numbers less re-derivable. Flagged in CLAUDE.md §9 for whoever touches it.

---

## AMENDMENT — 2026-09-12, recorded BEFORE the result was read

### This run is `informative`, not `evidence`. §7 does not fire on it.

Whatever this run reports, in either direction, it does **not** trigger
`PREREGISTRATION_V2.md` §7's abandonment of the strategy family, and it does not
satisfy §4.2's Stage 1 requirement. It is reported for information only.

**Why, stated before the numbers exist so this cannot read as moving the
goalposts afterwards.** Building this round surfaced five material defects in the
measurement apparatus, every one found by reading code rather than by a test or
an exception failing, and every one after the harness had been declared verified:

| # | Defect | Where | Would have shown as |
|---|---|---|---|
| 1 | Exit rule shaped the entry set (9/6/8 trades for rules §6 requires to share entries) | new code | six arms compared on six different entry sets |
| 2 | One DTE constant was really two (selection 3 vs pricing 4/252) | pre-existing | Strategy C silently re-priced: 44 legs/−$1,459 vs 41/−$516 |
| 3 | §3.3's "gamma stacked beyond" was vacuously true (54/54 cases) | new code | a conditional rule with no condition |
| 4 | Regime confirmation counted BARS, not minutes | pre-existing | regime 5× faster than registered, in the primary loss rule |
| 5 | Open positions discarded when bars ran out (36% of positions on truncated days) | pre-existing | flattered loss distribution — the exact quantity §6 protects |

Defects were still being found at roughly one per twenty minutes of looking when
the run was launched for the third time. There is no basis for asserting the next
twenty minutes would have found none. A number produced by an apparatus that has
been wrong five times is not evidence, and the §7 consequence — abandoning the
strategy family, irreversibly — is too severe to rest on it.

**The check that was asked for, and the one actually adopted.** The proposal was
review by "someone other than me". That was insufficient: Claude wrote
`PREREGISTRATION_V2.md` itself and had already made an error inside it (see the
§5 defect recorded below), so a second Claude reading the same artifacts is the
same kind of check, not an independent one. The adopted requirement is a
**different kind** of check — **known-answer tests**: synthetic sessions whose
correct outcome is computable by hand, one where the hypothesis must trigger and
win, one where it must trigger and lose, one where it must not trigger at all.
Defects found by measurement against a known answer, not by reading.

**Stage 1 evidence is the rerun after those tests pass.** That rerun gets its own
`evidence` entry in this log, and §7 attaches to it.

### Defects in `PREREGISTRATION_V2.md` §5 itself

**Both are for a FUTURE pre-registration to state correctly. Neither is a change
to v2** — §5's numbers do not move, and both clauses are left exactly as written.
They are recorded here because the same wording would otherwise be copied
forward.

Both concern the same clause, and both were found by running it rather than by
reading it.

#### Defect P1 — undefined for a negative strategy net

**§5's `shuffled_levels` criterion is undefined for a negative strategy net.**
It reads: *"`shuffled_levels` median ≤ 25% of strategy net."* Against a positive
net this is the intended test — the null arm must capture at most a quarter of
the edge. Against a **negative** net the inequality inverts and becomes
perverse: 25% of −$4,000 is −$1,000, so a null arm losing *more* than the
strategy satisfies it, and the criterion rewards the strategy for losing badly.

Treated here as **not met** whenever strategy net ≤ 0, reported as
`n/a — strategy net ≤ 0` rather than as a pass or as a numeric fail that looks
like it measured something. That is the conservative reading and it is what the
code does (`control._report`).

A future registration should state it in a form that holds in both directions —
as a ratio of gross profit, say, or explicitly conditioned on a positive strategy
net with a separate rule when net is negative. Claude wrote that clause and did
not notice the inversion; that is itself part of why a second Claude reading is
not the independent check this needs.

#### Defect P2 — the arm's trade count is not matched, so it flatters itself

**`shuffled_levels` traded 95 times against the strategy's 1,595** in the
informative run — **17× fewer** — and that, not skill, is most of why its net
(−$2,601) looks so much better than the strategy's (−$157,885).

The mechanism: the three forced-entry arms take a matched number of entries per
day (`n_per_day`), so they are comparable by construction — measured at 1,584 to
1,595 against the strategy's 1,595. `shuffled_levels` does not work that way. It
runs the REAL entry logic against a donor day's levels, so its trade count is
whatever that logic happens to produce, and spot is rarely within 0.075% of a
*wrong* boundary. Per trade the gap is much smaller: −$27 against −$99. The
strategy is genuinely worse per trade; it is not 60× worse.

Why this matters beyond one number: against a **losing** strategy, an arm that
trades less automatically looks better, so P2 and P1 compound. §5's clause
implicitly assumes a profitable strategy, where trading less means capturing less
edge. A future registration should either match the arm's trade count the way the
forced-entry arms are matched, or state the criterion per-trade rather than in
aggregate. As written the clause can be satisfied or missed for reasons that have
nothing to do with whether the levels carry information — which is the one thing
H1 asks.

### Known-answer tests — built, and they found a sixth defect

`tests/test_rangerev_known_answer.py`. Synthetic sessions simple enough that the
entry bar, direction, contract count, exit reason and dollar P&L are all
computable by hand, written out in each docstring and asserted to the cent:

| test | requirement | hand-computed answer |
|---|---|---|
| A | must trigger and **win** | entry 2.055 × 5 contracts, exit 2.475 on the magnet → **+$203.50** |
| B | must trigger and **lose** | −30% stop at spot 7490, +1 tick adverse, exit 1.325 → **−$371.50** |
| C | must **not** trigger | six cases: N regime, X regime, no magnet, never inside the window, rising off the wall, outside 09:45-14:30 |
| D | must trigger a known **count** | three separated approaches → exactly 3 entries, each +$203.50 |

**They failed on first run, and the gap was exactly one side of commission.**
$206.75 against $203.50, and −$368.25 against −$371.50: both off by
5 × $0.65 = $3.25.

**Defect #6 — commission was charged on one side only.** Spec §10 and
CLAUDE.md §5.9 both say **$0.65 per contract per side**, so a round trip is
$1.30. Every simulator subtracted a single side at exit: `daysim` (Strategy C),
`rangesim` (round three), both `control` forced-entry arms, and both `shadow.py`
paths — the last of which are **operator-facing paper P&L**. The half-spread was
correctly paid twice (it is inside the fill prices); only commission was halved.

Fixed in one place, `FillModel.round_trip_commission()`, with a test asserting all
four simulator call sites use it and none holds a literal rate. Correct for
partial exits too: 2× on each closed contract allocates the entry commission
pro-rata, and closed contracts sum to the position.

**Coverage was then extended past the strategy logic**, to the arithmetic that
had none. Two areas stood out as untested despite 234 passing tests:

- **The Stage 1 verdict itself** (`control.stage1_criteria`, extracted from the
  console-printing `_report` so it could be tested at all). This is the code that
  decides whether the hypothesis lives, and a wrong p-value here produces a
  confident verdict nothing downstream would contradict. Now pinned with
  hand-computed p-values at the 0.05 boundary — 1-of-20 ties, since a tie counts
  as the random arm winning — both criteria sitting exactly on their thresholds,
  and the negative-net inversion asserted to be refused even though the
  arithmetic satisfies it.
- **The §6 loss distribution** — the quantity §6 says every report must lead
  with. Hand-computed across ten trades, with the worst-DOLLAR trade
  deliberately different from the worst-PERCENT trade so a confusion between
  them cannot pass by proportionality.

**Every mutation aimed at these is caught** — commission side, entry half-spread,
stop slippage, proximity window, approach direction, null-arm sizing, null-arm
dropped positions, tie handling, all-arms-vs-any, the 25% threshold, the
negative-net guard, the median criterion being ignored, worst-from-wins, `<` vs
`<=` at −40%, and ties counted as wins. 18 known-answer tests, 255 in total.

**Two lessons about the technique itself**, both recorded in CLAUDE.md §10.1:

- A mutation applied to the wrong one of two identical lines produces a green
  test that is indistinguishable from a working check. `replace(old, new, 1)` hit
  Strategy C's forced-entry arm instead of round three's and reported "STILL
  GREEN"; re-aimed, it went red at once.
- A boundary needs a fixture value sitting **on** it. The loss-distribution
  fixture contained no break-even trade, so `x > 0` and `x >= 0` were
  indistinguishable and a mutation turning ties into wins passed.

**Five mutations confirm the known-answer tests bite**, each reverted in turn and
each turning a test red: commission back to one side, half-spread not paid on
entry, stop slippage removed, proximity window widened to 0.20%, approach
direction ignored. This is the class of check that was missing — the five earlier
defects were all found by reading, and a property test passes while the
arithmetic underneath is wrong.

### Consequence for the informative run currently executing

It was launched **before** defect #6 was found, so its costs are understated by
$0.65 per contract per trade. Its net P&L is **optimistic by exactly
`trades × contracts × $0.65`** — a known quantity in a known direction, not an
unknown error. It is reported anyway, per instruction, with that caveat attached.
It is not evidence and §7 does not fire on it.

### RESULT of the informative run — 2026-09-12

`STAGE 1 FAIL`, decisively. 974 replayable sessions, 1-minute bars, 20 seeds,
DEFAULT `RangeParams()`, primary arm `regime_flip`/`magnet`/3 DTE.

| criterion | required | observed | verdict |
|---|---|---|---|
| P(random ≥ strategy) · `matched_time` | ≤ 0.05 | **1.000** | FAIL |
| P(random ≥ strategy) · `matched_dir` | ≤ 0.05 | **1.000** | FAIL |
| P(random ≥ strategy) · `full_random` | ≤ 0.05 | **1.000** | FAIL |
| P(random ≥ strategy) · `shuffled_levels` | ≤ 0.05 | **1.000** | FAIL |
| `shuffled_levels` median ≤ 25% of strategy net | n/a — strategy net ≤ 0 | −$2,601 vs −$157,885 | NOT MET |

All 80 random runs beat the strategy. Strategy −$157,885 at PF 0.40; the three
matched-count arms −$75.9k to −$78.3k on 1,584–1,595 trades against the
strategy's 1,595.

**§6 loss distribution, first:** 1,595 trades · 991 losses (62%) · worst −101% ·
5th pct −70% · median loss −18% · worst $ −$1,191 · 203 losses ≤ −40% · win 38%.

**Six §3.2 × §3.3 arms, all on identical entries (1,595 each):**
regime_flip/magnet −$157,885 · regime_flip/continuation −$168,483 ·
hard_stop/magnet −$156,101 · hard_stop/continuation −$162,005 ·
time_stop/magnet −$89,241 · time_stop/continuation −$88,074.
Magnet exits 381/1,595 (24%), so the target is not inert at scale — the 12-day
smoke that showed it never firing was simply too small.

**Why this is informative and not evidence — three defects found in the result
itself:**

1. **Every mark was a model price.** 6,552,313 marks, 100% Black-Scholes
   fallback at a flat 16% IV, `file: 0 rest: 0`. Defect #8, in the driver: it
   pre-built its days and never attached a `MarkFetcher`, bypassing the one
   `run_control` creates. §5.10's provenance tracking DID flag it — into
   `run_meta.json`, unread until after the run.
2. **The −101% worst loss is impossible.** Defect #7: `FillModel.sell` returned
   NEGATIVE fill prices for cheap options (mid 0.02, stop-triggered → −0.055), so
   a long position lost more than its premium. Now floored at zero.
3. **Costs were understated by one commission side** (defect #6), fixed after
   launch.

**All three are common-mode** — the same mark function, cost model and floor
apply to the strategy and the null arms alike — so the p-values are considerably
more robust than the dollar figures. p = 1.000 on four arms is a large margin for
common-mode corrections to reverse. That is a reading, not the finding.

**−$157,885 is not a tranche return.** It is 1,595 independently-sized positions
at 40% of tranche with unbounded concurrency (interpretation choice #3). The
meaningful figure is **−$99 per trade, ≈ −8% of premium**.

**§3.3 is not answered by this run.** The "gamma stacked beyond the magnet"
precondition held on **97.4% of bars** (347,672 of 356,911), so after the fix to
defect #3 the continuation rule went from 100% unconditional to 97%
unconditional. The gap between the two target columns is mostly "hold past the
magnet" versus "don't", not "hold when stacked" versus "don't".

_Per the amendment above, §7 does not fire on this._

---

## 2026-09-12 — Stage 1, round three · `evidence`

**Logged at launch, before any result exists.**

| field | value |
|---|---|
| label | **`evidence`** — §7 attaches to this run |
| command | driver `stage1.py`, equivalent to `control --start 2022-01-03 --end 2025-12-31 --seeds 20 --bar-minutes 1 --strategy range --loss-rule regime_flip --target magnet --dte 3` |
| purpose | `PREREGISTRATION_V2.md` §4.2 Stage 1, on a harness that now passes known-answer tests |
| partition | Development, 2022-01-03 → 2025-12-31. Checkpoint and holdout untouched |
| parameters | **DEFAULT** `RangeParams()` — 0.075% proximity, −30% hard stop, 60-min time stop, 3 DTE. Nothing tuned |
| resolution | 1-minute bars · 20 seeds · 4 null arms |
| primary arm | `regime_flip` / `magnet`, designated in advance; the other five are descriptive |
| marks | REST `MarkFetcher` attached; the write is REFUSED above 25% model marks |

**What is fixed relative to the informative run:** #6 commission per side, #7
sell-fill floored at zero, #8 marks actually attached — plus the guard that made
#8 unrepeatable: `replay.assert_marks_are_real` now RAISES rather than warns, and
`run_control` refuses prebuilt days without the builder that built them, which
was the structural bypass. The driver also fails fast when no API key is present
rather than building for an hour toward a refusal.

**Unchanged and still carried:** the 95-of-974 truncated sessions, the absence of
wick information on REST-sourced days, interpretation choices 1-4, and prereg
defects P1 and P2.

### RESULT — 2026-09-12 · **STAGE 1 FAIL**

974 replayable development sessions · 1-minute bars · 20 seeds · DEFAULT
`RangeParams()` (0.075% proximity, −30% hard stop, 60-min time stop, 3 DTE) ·
primary arm `regime_flip`/`magnet` · **86.7% real NBBO** (REST 5,671,639 marks,
model fallback 866,517 = 13.3%, under the 25% refusal limit).

#### §5 Stage 1 criteria

| criterion | required | observed | verdict |
|---|---|---|---|
| P(random ≥ strategy) · `matched_time` | ≤ 0.05 | **1.000** | **FAIL** |
| P(random ≥ strategy) · `matched_dir` | ≤ 0.05 | **1.000** | **FAIL** |
| P(random ≥ strategy) · `full_random` | ≤ 0.05 | **1.000** | **FAIL** |
| P(random ≥ strategy) · `shuffled_levels` | ≤ 0.05 | **1.000** | **FAIL** |
| `shuffled_levels` median ≤ 25% of strategy net | n/a — strategy net ≤ 0 (defect P1) | −$3,994 vs −$138,050 | **NOT MET** |

**All 80 random runs beat the strategy.** Not one of 20 seeds × 4 arms failed to.

| arm | median net | p5 | p95 | median PF | trades |
|---|---|---|---|---|---|
| strategy | −$138,050 | — | — | 0.47 | 1,595 |
| `matched_time` | −$73,452 | −$80,243 | −$61,706 | 0.44 | 1,588 |
| `matched_dir` | −$74,227 | −$85,935 | −$66,044 | 0.44 | 1,595 |
| `full_random` | −$72,903 | −$86,410 | −$64,215 | 0.44 | 1,595 |
| `shuffled_levels` | −$3,994 | −$12,027 | +$6,389 | 0.73 | 95 |

Per trade: strategy **−$87**, matched-random arms **−$46**. The GEX entry loses
**about 1.9× what a random entry loses** at the same times, directions and trade
counts. H1 is not merely unsupported — the levels are worse than chance.

#### §6 loss distribution, reported first

1,595 trades · 965 losses (60.5%) · worst **−97%** · 5th pct −66% ·
median loss −18% · worst $ −$1,344 · **190 losses ≤ −40%** · win rate 39%.

The worst is −97%, not the informative run's impossible −101%: defect #7's
sell-fill floor now holds losses at or above −100% of premium.

#### Six §3.2 × §3.3 arms — identical entries (1,595 each), so §6 is satisfied

| loss rule | target | net | per trade | median loss | magnet exits |
|---|---|---|---|---|---|
| regime_flip | magnet | −$138,050 | −$87 | −18% | 381 |
| regime_flip | continuation | −$149,176 | −$94 | −17% | 324 |
| hard_stop | magnet | −$171,065 | −$107 | −33% | 443 |
| hard_stop | continuation | −$189,307 | −$119 | −33% | 387 |
| time_stop | magnet | −$88,313 | −$55 | −14% | 295 |
| time_stop | continuation | −$88,345 | −$55 | −14% | 227 |

**No loss rule and no target rescues it.** The best of six still loses $55 a
trade. `hard_stop` is the WORST, which is the opposite of §3.2's stated concern
that waiting for a regime flip is too slow: cutting losses at −30% did not help,
it hurt, because the stop pays an extra tick and real NBBO spreads on cheap
options are wide. `hard_stop` also got worse once real marks replaced the flat
$0.10 model spread (−$171k against −$156k), while `regime_flip` got better.

#### What still qualifies this result

- **Prereg defect P2 is unresolved and visible here:** `shuffled_levels` traded
  95 times against 1,595 — 17× fewer — so its −$3,994 is mostly volume. Per
  trade it is −$42, slightly BETTER than the matched-random arms' −$46 and far
  better than the strategy's −$87. The arm's verdict (p = 1.000) does not depend
  on this, since the strategy lost to it on any reading.
- **§3.3 is still not answered.** The continuation precondition held on 97.4% of
  bars, so the rule remains near-unconditional. What the table above shows is
  that holding past the magnet is worse, not that stacked gamma is uninformative.
- **−$138,050 is not a tranche return** (interpretation choice #3): 1,595
  independently-sized positions at 40% of tranche, unbounded concurrency. The
  per-trade figures are the comparable ones.
- **95 of 974 sessions (9.8%) end at 14:59 ET**, and REST-sourced days carry no
  wick information.

#### §7

`PREREGISTRATION_V2.md` §7: *"The strategy family is abandoned — not retried — if:
Stage 1 fails on the development set."* **It failed, on a harness that passes
known-answer tests, with real marks, correct per-side costs, a floored fill
model, and identical entries across every arm. The criterion is met and §7
applies.**

This is the third hypothesis tested against this data and the third to fail. Per
§7 the honest conclusion is that the platform's value is observability and exit
management, not autonomous entry, and the project stops looking for an entry edge
in dealer-gamma levels.

Per CLAUDE.md §4: **stopping is a successful outcome.** The cost was a few weeks
and no tranche.

### Findings that affect what any future run can claim

1. **`index_values` carries one print per minute on REST-sourced days.**
   `index_rest.fetch_index_day` keeps the aggregate's close and discards its
   open/high/low, so a 1-minute bucket holds a single value. Minute bars
   therefore have **no wick information**: high/low degenerate to
   max/min(open, close). 2023's 165 flat-file days do carry true sub-minute
   prints and get real intrabar ranges. Any minute-resolution result must be
   read knowing that ~85% of days have reconstructed rather than observed bar
   extremes.

2. **85 development-set days in 2022 are truncated at 14:59 ET.** The upstream
   flat files are capped at 19:59 UTC, which is 15:59 ET in EDT (harmless) and
   14:59 ET in EST (the last hour missing). Affects Jan–mid-Mar and Nov–Dec
   2022 — 85 of 996 development days, 8.5%. **Not repairable:** the REST
   aggregates endpoint returns no 2022 index data (measured), and raw flat
   files are deleted after conversion per spec §18. Those days end before the
   15:50 EOD flat, so any position still open simply runs out of bars.

3. **ATR is scale-dependent and had to be held still deliberately.** Measured
   on 2025-04-08: the naive 30×1-minute ATR is 7.0 points against the 6×5-minute
   figure of 22.4. Since `CParams.trail_atr_mult` and `trail_min_pts` are
   frozen, `synth.atr30` now coalesces back to 5-minute ranges before
   measuring. Without that, minute replay would have tightened every trail
   roughly threefold on the most volatile days.
