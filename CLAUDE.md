# CLAUDE.md — gexbot

Persistent context for Claude Code sessions in this repo.
Baseline: commit `432d0af`+, 2026-09-11. Facts marked *(verify)* may drift.

---

## 1. What this is

`gexbot` is a research-and-paper-trading apparatus for an intraday SPX/XSP
options strategy keyed off **dealer gamma exposure (GEX)**.

**Thesis.** Aggregating dealer gamma by strike yields price levels — flip
point, G_MAX, put wall, call wall — where dealer hedging either suppresses
moves (positive gamma → pinning) or amplifies them (negative gamma →
trapdoors). Trade around those levels.

**It is not a bot that makes money.** As of today it is an *instrument for
deciding whether the thesis is true*, and its current best evidence says
probably not (§2). Treat it as a measurement device. The apparatus is the
asset; the strategy is the hypothesis under test.

Scale is deliberately small: $3,000 tranche, ~$3,600/yr fixed-cost hurdle
(data + commissions) that must be cleared before any real money moves.

---

## 2. Current status — READ BEFORE PROPOSING ANYTHING

**Phase:** round two, pre-registered and frozen at git tag `prereg-v1`
(2026-09-09). **The Stage 1 signal test has not been run yet.** That is the
next real move.

### Validation history — two failures

1. **Mean-reversion / bounce book lost money outright: −$5,082.** Not
   marginal. Negative.
2. **Breakout book produced a textbook false positive.** Green all four
   years, OOS PF 1.66, beat all four null models at p<0.05 on 2023–2026,
   with the `shuffled_levels` arm collapsing to an $11 median (commits
   `7af1c8f`, `1da38b8`). Then **2022 — the one period never touched —
   showed no edge at all**: random entries beat the strategy 50–95% of the
   time.

**Why #2 happened.** Parameters had been fitted on 2023–2026, so running the
control experiment on 2023–2026 was partly *guaranteed* to pass. The p<0.05
measured the fitting, not the market. It was detectable only because one
period had been held clean. Round one also ran 15 parameter sweeps *before*
ever asking whether the entry beat chance — exactly backwards.

`gexbot/watch.py` narrates shadow trades with a permanent disclaimer for
this reason: *"remembering only the good calls is how false confidence
forms."*

### What is nonetheless solid

A pipeline that survives 7-billion-row days; ledgers, greeks, entry/exit/
discipline engines (55 tests passing *(verify)*); honest REST NBBO marks
with provenance tracking; a backtest runner with era splits and executable
gates; a four-arm control harness; put-call-parity spot reconstruction for
pre-2023; Discord alerting; a results explorer. None of this is invalidated
by the negative result — it is the apparatus that *produced* the negative
result, which is the point.

---

## 3. The rule that overrides everything

> **No parameter tuning, no sweeps adopted, no config edits to strategy
> parameters until the Stage 1 signal test passes on the development set
> with DEFAULT parameters.**

`PREREGISTRATION.md` §4: *"If this fails, the hypothesis is rejected and no
tuning is permitted."* Tuning is not disfavoured here — it is structurally
unavailable until a result that does not yet exist is produced.

If asked to "improve the numbers," "optimize," "find better parameters," or
"see what setting works best" — **stop and say this rule out loud.** The
legitimate moves are: run the pre-registered test, improve engineering
(execution, fills, feed gaps — always safe), retire mechanisms shown inert,
or accept the kill criteria. Say so plainly rather than quietly complying;
if the user reaffirms after hearing the constraint, that is their call and
you proceed — but the constraint gets stated first, every time.

---

## 4. Research methodology (PREREGISTRATION.md — FROZEN at `prereg-v1`)

### Data partition — no exceptions
| Partition | Dates | Use |
|---|---|---|
| **Development** | 2022-01-03 → 2025-12-31 | Unlimited exploration, sweeps, iteration |
| **Checkpoint** | 2026-01-02 → freeze date | **ONE run, at the end.** Weak evidence (partly seen in round one) |
| **True holdout** | Every session after the freeze | Paper trading — the real out-of-sample test |

- 2021 excluded entirely (quote coverage too thin; 57% model marks).
- 2022 is now *inside* development — it was round one's clean holdout and is
  burned. Its value as a fresh test is spent.
- Data that does not exist yet cannot be overfitted. Forward paper trading is
  not merely operational validation — **it is the experiment.**

### Method order — frozen
1. **Signal test first.** Control experiment, 4 null arms, ≥20 seeds, on the
   development set, **DEFAULT parameters**.
2. Only if signal is established does any tuning happen.
3. Control experiment **repeated** after tuning, to confirm the edge is not
   an artifact of the tuning.
4. Checkpoint run. Once.
5. Paper trade.

### Success criteria — these numbers do not move
- **Stage 1 (signal):** P(random ≥ strategy) ≤ 0.05 on **all four** null
  arms, AND `shuffled_levels` median ≤ 25% of strategy net.
- **Stage 2 (after tuning):** ≥300 IS trades, ≥100 OOS trades; OOS PF ≥ 1.3;
  OOS maxDD ≤ 20%; annualised P&L clears $3,600; PF excluding top 5 trades
  ≥ 1.15; control re-passes at p ≤ 0.05.
- **Stage 3 (checkpoint 2026):** PF ≥ 1.2 and positive net. A miss means back
  to development **with a new holdout** — not a tweak.
- **Stage 4 (paper):** ≥20 sessions, ≥30 trades, signal parity clean, P&L
  direction consistent with development. Live money requires all four.

### Hypotheses stated in advance
- **H1 (primary):** GEX levels from classified intraday flow carry
  information absent from matched-random entries. Sharpest test:
  `shuffled_levels` must underperform — same logic, same market, wrong walls.
- **H2:** that information survives realistic costs.
- **H3:** if H1 holds on some sub-periods only, the gate must be specified
  *in advance* (VIX regime, 0DTE share, spread width, RV-IV spread).
- **Explicitly NOT hypotheses:** that mean-reversion at walls works (it lost
  money); that momentum at walls works (in-sample only); that the C.5 exit
  machinery adds value (sweeps showed exits were inert).

### Anti-fooling rules
- **Log every run** to `docs/RUNLOG.md`: date, command, purpose, result.
  Retrospective narrative is not permitted.
- **Label every run** `development` or `evidence` **at launch**. A run whose
  parameters change afterwards was development, always.
- **≤6 tuned parameters total.** Round one fitted ≥5 and still failed.
- **Prefer plateaus, refuse boundaries.** An edge-of-grid value is not
  adopted — extend the grid or leave the parameter at default. Round one
  returned edge-of-grid "plateaus" three times, which actually meant the
  exits had gone inert.
- **Inert parameters get removed, not adopted.** If a value makes no
  difference across its range, delete the mechanism.

### Kill criteria — agreed in advance
Stop, or abandon the strategy family, if: Stage 1 fails; or Stage 2 needs >6
tuned parameters; or Stage 3 fails after a Stage-2 pass twice with different
approaches; or paper trading shows unexplainable signal-parity failures.

**Stopping is a successful outcome.** The cost of a negative result is a few
hundred dollars and some weeks. The cost of ignoring one is the tranche.

### Changing any of the above
Requires a **new** pre-registration document that supersedes the current one
and **starts a fresh holdout**. Never edit `PREREGISTRATION.md` in place.

---

## 5. Engineering constraints — never violate

**Experimental integrity**
1. No checkpoint or holdout result may cause a parameter change. If it does,
   the strategy re-enters development and needs a new holdout.
2. The nightly analysis module has **no write access to the parameter
   config** (spec §18.1). No code path exists, and a test asserts it. Nightly
   self-tuning is the round-one failure mode at maximum frequency on minimum
   sample. Do not add a "propose-and-apply" path, an A/B self-selector, or
   any between-session "learning."
3. `sweep.py` **never writes config.** It prints evidence; a human edits the
   versioned parameter file afterward.
4. Change proposals must cite ≥40 trades, name a pre-registered question, and
   pass a control re-test before adoption.
5. MFE-after-exit is reported **paired with** its opposite (trades where
   holding longer gave back profit), always both columns or neither.
   Unpaired, it biases every review toward looser exits — which the round-one
   sweeps showed leads to no exits at all.

**Strategy code**
6. `entries.py`, `exits.py`, `discipline.py` are **pure logic, no I/O**, so
   backtest and live run the *same brain*. This is what makes signal-parity
   checks meaningful. Breaking it destroys the only forward test available.
7. `discipline.py` has **no override method, by design.** Do not add one.
   Halting is one click; **re-arming is a config-file edit only.**
8. Block flow is a **measurement, not a signal.** Nothing in entries/exits
   reads it. It is logged so a pre-registered question can judge it *before*
   it is ever allowed to gate a trade.

**Data honesty**
9. Costs stay pessimistic: $0.65/contract/side, 50% of the half-spread each
   side, +1 tick adverse on stop exits. Anything that only works at midpoint
   fills does not work.
10. Mark provenance is tracked and reported (file / REST / model-fallback %)
    on every run. The BS fallback is **always flagged so fiction never
    hides.** Never silently substitute a model price for a real quote.
11. Reconstructed data is source-flagged (`source='parity'`) so no result
    silently mixes provenance.

**Operations**
12. Monitoring failure ≠ trading failure. Separate process; drop-oldest
    queue; the feed thread **never raises into the caller**; reconnects with
    backoff; reports staleness so consumers halt on their own terms rather
    than trusting silent data.
13. The dashboard is **read-only**. The only control it exposes is HALT. No
    entries, no parameter edits, no re-arm from the UI.
14. **Localhost only** (127.0.0.1). This box holds brokerage credentials. If
    remote viewing is wanted: Tailscale or SSH tunnel, never a port-forward.
15. One source of truth: the dashboard reads the same state store the
    backtest analyzer reads. No dashboard-only calculations.
16. Shadow/paper output is always visually tagged (📄) and carries the
    disclaimer. A phone glance must never confuse simulated with real.
17. Backfill halts itself if drive free space drops under 200 GB. At most one
    raw day ever sits on disk — raw files are deleted immediately after
    conversion.

---

## 6. CLI

All commands: `python -m gexbot <cmd>`. Dates are ISO (`2024-01-02`).

| Command | Key flags | What it does |
|---|---|---|
| `backfill` | `--dataset {opra_trades,opra_quotes,index_values}` (repeatable), `--start`, `--end` | Download + convert flat files. Resumable, Ctrl-C safe (at most current day repeats). Default order: trades → quotes → indices. |
| `status` | — | Print manifest summary (dataset/state/days/rows/GB). |
| `index-rest` | `--start`, `--end` | Fast index backfill via REST aggregates instead of 2.3 GB/day flat files. Same output schema. |
| `parity` | `--start`, `--end` (required), `--overwrite` | Reconstruct SPX spot from option trades via put-call parity — unlocks 2021–22. |
| `backtest` | `--start`, `--end` (required), `--tranche` (3000) | Replay a range through Strategy C, report §10 gates, write a results bundle. |
| `control` | `--start`, `--end` (required), `--seeds` (20), `--tranche` | **The Stage 1 test.** Null-model experiment: does the GEX entry beat random? |
| `sweep` | `--start`, `--end`, `--param`, `--values` (required), `--tranche`, `--full-strategy` | Parameter grid → gate frontier. Breakout-only by default. Reports plateaus vs peaks. **Gated behind Stage 1.** |
| `levels` | `--underlying` (I:SPX), `--model {naive,short_all}`, `--expiries` (2), `--window` (0.06), `--per-1pct` | Print today's GEX map from a live chain snapshot. |
| `watch` | `--underlying`, `--interval` (180s), `--expiries`, `--window`, `--tranche`, `--no-shadow`, `--once` | Market-hours structural alerts + shadow trade narration to Discord. REST snapshots, not the live engine. |
| `explore` | — | Streamlit results explorer on `GEX_DASHBOARD_PORT`, bound 127.0.0.1. |
| `discord-test` | — | Send one test message to each configured webhook. |

**Undocumented env switch:** `GEX_BREAKOUT_ONLY=1` forces breakout-only mode
in `backtest` and `control` (read at `control.py:98`, `backtest.py:52`). It is
*not* in `.env.example`. Be explicit about its value when reporting any run —
it silently changes what was tested.

---

## 7. Module map

```
gexbot/
  config.py       Typed pydantic settings — single source of truth.
                  No hardcoded paths/ports/parameters anywhere else.
  clock.py        THE timezone authority. UTC ns <-> ET minute-of-day via
                  zoneinfo, scalar + vectorized + inverse. Never reintroduce a
                  hardcoded UTC offset; import from here.
  pipeline.py     S3 flat-file download → filtered zstd Parquet. KEYS = S3 templates.
  manifest.py     DuckDB manifest; makes backfill resumable.
  symbols.py      OCC root parsing.
  index_rest.py   Index minute bars via REST aggregates (fast path).
  marks_rest.py   Per-contract NBBO via REST + permanent disk cache (honest fills).
  parity.py       Put-call-parity SPX spot reconstruction for pre-2023.

  greeks.py       Black-Scholes price/gamma.
  ledger.py       Per-strike dealer gamma; flip/G_MAX/walls; BlockFlowTracker.
                  3 baselines: naive | short_all | flow_only.
  levels.py       Live GEX map from chain snapshot (OI-based, public-comparable).
  livefeed.py     WebSocket ingester + tick-rule classifier + FlowLedger + combine().
                  OI alone is blind to 0DTE (~half of SPX volume); this is the fix.

  entries.py      EntryEngine: C.4 bounce/breakout, C.9 flip, C.11 reversal score
                  + macro veto. Logs BLOCKED signals (filter-effectiveness dataset).
  exits.py        ExitEngine: trail/PT1/hard-stop/level-invalid/time/EOD/regime.
  discipline.py   Anti-revenge state machine. No override method.
  costs.py        FillModel — pessimistic by design.

  synth.py        Synthetic bars — a test fixture with controllable shape, NOT a
                  market model.
  daysim.py       DaySimulator: binds entries+exits+discipline+fills over 5-min bars.
  replay.py       Parquet day → Providers for DaySimulator. The bridge from
                  synthetic proof to real backtest.
  backtest.py     Range runner: era splits, §10 gates, results bundle.
  metrics.py      BacktestReport + the §10 gates (the judge).
  sweep.py        Parameter grid → gate frontier. Never writes config.
  control.py      Four null arms: matched_time, matched_dir, full_random,
                  shuffled_levels.
  notify.py       Discord, 3 tiers, fire-and-forget, drop-oldest.
  watch.py        Market-hours monitor + shadow narration.
  explore.py      Streamlit results explorer. Read-only by design.
```

**Key defaults** (`SimConfig` / `EntryParams` / `CParams` / `CostParams` /
`DisciplineParams` / `GateParams`): tranche $3,000, premium budget 40%, XSP =
SPX/10, entry window 09:45–14:30, PT1 +30% (sell half), hard stop −25%, trail
1.5×ATR30 (1.0× after PT1, clamped 8–40 pts), time stop 3 h without PT1, EOD
flat 15:50, max 3 trades/day (5 on range days), 2 losing trades ends the day,
30-min stop-out lockout, −15% of tranche halts.

---

## 8. Setup, environment, tests

```bash
uv venv && source .venv/bin/activate && uv pip install -e ".[dev]"
cp .env.example .env          # fill Massive keys + GEX_DATA_ROOT
python -m pytest -q           # 55 passing
ruff check .                  # line-length 100
```

Python ≥3.12 (venv is 3.14). Stack: polars, duckdb, pydantic-settings, httpx,
websockets, rich, streamlit, boto3.

`.env` holds Massive API + separate S3 flat-file creds, `GEX_DATA_ROOT` (the
Thunderbolt drive), Discord webhooks, Schwab keys (unused until the app is
live), ports 8741/8742. **Never commit `.env`; never send creds anywhere.**

---

## 9. Known gaps

**Open**

- **`docs/HANDOFF.md` does not exist** — not on disk, not in git history —
  yet `watch.py:11` cites "HANDOFF §2" as the canonical account of the two
  validation failures. This file (§2) is currently the best substitute.
- **`docs/RUNLOG.md` does not exist**, though prereg §5 makes it mandatory
  for every run. **Create it with the first round-two run.**
- **Drawdown gate — decided 2026-09-11.** `metrics.py:GateParams` keeps its
  **12%** default deliberately; it is the stricter number and it stays.
  Round-two evidence runs are to be judged at the frozen §4 Stage-2
  criterion of **20%**, passed explicitly as
  `GateParams(max_drawdown_frac=0.20)`. Do not "reconcile" these by editing
  the default.
  **Caveat: that is policy, not yet plumbing.** `backtest.py:83` and
  `sweep.py:93` both construct `GateParams()` internally with no override,
  and no CLI flag exists, so today a run *cannot* pass 0.20. Wiring a
  `--max-dd` flag through both call sites is the outstanding task; until it
  lands, record in RUNLOG which threshold a run was actually judged at.
- `replay.py` still has a dead placeholder branch in the OI-loading path
  (a `type(led).load_oi.__self__ if False else None` no-op, immediately
  followed by `led.strikes.clear()` and a real bulk load).
- `fix_levels_units.py` remains at repo root — a stray one-off script, not a
  duplicate module. Tracked. Review and delete when convenient.

**Fixed 2026-09-11** (kept here so the failure modes stay visible)

- ~~Timezone~~ — three modules hardcoded three different UTC offsets
  (`replay.py` -5, `parity.py` -4, `marks_rest.py` -4), so the same instant
  bucketed into different bars depending on which module read it, and every
  date on the wrong side of a DST boundary shifted by an hour — inside the
  09:45-14:30 entry window. All three now import `gexbot/clock.py`.
  Found while fixing: `dt.hour()` returns Int8 in polars, so the vectorized
  `hour * 60` silently overflowed (9*60 → 28) until cast to Int64. The old
  `test_replay_metrics` fixture stamped its timestamps with the same -5 the
  replay used, so two compensating bugs read as correct — the fixture now
  builds true ET timestamps. `tests/test_clock_control.py` covers a summer
  and a winter date in both directions.
- ~~`shuffled_levels` self-donation~~ — the arm shuffled day indices without
  guaranteeing a derangement, so ~1 day in e kept its own walls and silently
  ran the real strategy inside the null arm, biasing H1's sharpest test
  toward passing. Now `control.derangement()`, with a warning when a
  single-session range makes the arm meaningless.
- ~~Stale root duplicates~~ — nine modules shadowing `gexbot/` (four
  identical, four diverged) deleted. `top_level.txt` was already `gexbot`
  only, so nothing imported them.

## 10. Working agreements for sessions in this repo

- **State the §3 rule before any work that touches strategy parameters.**
- Log every backtest/control/sweep run to `docs/RUNLOG.md`, labelled
  `development` or `evidence`, at launch.
- Report results faithfully: if a gate fails, say so with the numbers; if a
  run was skipped or a day had missing data, say that. Never soften a
  negative result — the negative result is the product here.
- Engineering fixes (execution, slippage, fill quality, feed gaps, tz bugs,
  crashes) are **always safe to make immediately** — they are engineering,
  not strategy.
- Prefer deleting inert mechanisms over tuning them.
