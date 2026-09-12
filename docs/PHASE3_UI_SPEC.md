# Phase 3 Spec — Operational UI

**For:** Claude Code, implementing against `/Volumes/Main Drive/development/GexDev`
**Prerequisites:** `CLAUDE.md`, and Phases 1–2 complete (state store + API).
**Depends on:** the live flow overlay proven non-zero during market hours.

---

## 0. Scope and stance

A local operator dashboard for watching the gamma map and the bot's state
during a session, plus intraday history. Functional styling first; a design
pass comes later.

**What this UI must never do**, however tempting the reference products are:

- No directional calls. No "bullish bias," no sentiment badge, no
  "buy on dips," no pin-probability forecasts. GEX is a volatility-regime
  measure, not a direction signal, and our own testing found the entry logic
  did not beat random out-of-sample (`CLAUDE.md` §2). A dashboard that
  asserts direction every time it is opened will shape trading whether or
  not it is right.
- No control over trading. Read-only, per Phase 2 §4.3. A HALT button may
  be added later; nothing else.
- No numbers we cannot source. Every figure on screen traces to a stored
  poll or a computed quantity with a defined formula.

Labels follow the gamma **sign**, never convention: positive gamma below
spot is `SUPPORT`, negative is `TRAPDOOR`; above spot, `RESISTANCE` and
`LAUNCHPAD`. (`gexbot.levels.level_label` is the single authority — the UI
must not reimplement it; expose it through the API.)

---

## 1. Stack

- Vite + React + TypeScript, Tailwind, Apache ECharts (`echarts-for-react`),
  `motion` (motion.dev) for transitions only — no decorative animation.
- Dev server on `GEX_DASHBOARD_PORT` (8741), proxying `/api` and `/ws` to
  `GEX_API_PORT` (8742). Bind `127.0.0.1`.
- Lives in `ui/` at the repo root. Node deps stay out of the Python package.
- Dark theme only. No login. No remote access (Lightsail comes later, and
  will need auth before it is exposed — out of scope here).

---

## 2. Screen 1 — Live (default route `/`)

### 2.1 Header strip
`SPX 7,657` · `net −12M` · `OI −12M · flow +0M` · feed state (live /
reconnecting / off) · `as of HH:MM:SS` with age in seconds.

If `/api/health` reports `ok: false` or the poll is older than 5 minutes
during market hours, the whole strip turns amber and reads `STALE — last
poll 11m ago`. Silent staleness is the failure mode this guards.

### 2.2 Strike profile (main panel)
Horizontal bars, strikes on the vertical axis descending, zero line centred.

- Source toggle: **flow** (combined OI + today's flow, default) · **OI**
  (baseline only) · **volume** (unsigned day volume per strike, context
  only — label it "activity, not positioning").
- In flow mode each bar is stacked: OI component and flow component in
  distinguishable shades, so today's contribution is visible at a glance.
  This is the view no public product offers; it earns the top of the screen.
- Spot drawn as a horizontal line with a price tag.
- Annotated levels drawn as dashed horizontal lines with inline labels:
  flip, put wall / call wall (labelled by sign per §0), max accelerator,
  max magnet, first positive above.
- Hover a bar → strike, combined gex, OI component, flow component,
  distance from spot in points and percent.

### 2.3 Right rail (three stacked cards)

**Nearest structure** — up to 5 rows, ranked by |gex|, each showing strike,
sign-derived label, distance in points, and gex. This answers "what is
near me" faster than the chart does.

**Regime** — `positive gamma` / `negative gamma` / `neutral (dead zone)`
with the mechanical description used in alerts. Uses the same dead-zone
threshold as `watch.py`, read from the API, not reimplemented.

**Shadow position** — the open shadow trade (contracts, strike, expiry,
entry premium, entry spot, current spot, trigger level) or `flat` with the
blocking reason. Same text the Discord footer produces.

---

## 3. Screen 2 — Session (`/session/:date`, default today)

Four stacked panels sharing one x-axis (session time) and one brushable
range selector. All read `poll_snapshot` / `poll_strike` via the API.

### 3.1 Net gamma over time
Line of `net_gex`, zero line marked. In flow mode, also plot `oi_net` and
`flow_net` as separate series so it is visible how much of the day's change
came from today's trading versus greeks recalculating against a moving spot.

### 3.2 Level drift vs spot
Spot, flip, put wall, call wall as lines over the session.

This panel exists for a specific diagnostic: a level that tracks spot is an
artifact, not structure. The 2026-09-11 flip-alert noise was exactly this,
and it cost several confusing alerts before it was understood. Make it
obvious.

### 3.3 Premium drift by side
Cumulative premium (`price × size × 100`) accumulated separately for calls
and puts through the session, from classified prints.

- Plot **four** series, not two: customer-bought calls, customer-sold calls,
  customer-bought puts, customer-sold puts. Unsigned totals conflate "paid
  $9.6M for puts" with "sold $9.6M of puts", which are opposite.
- Mark crossovers (net call premium overtaking net put premium, or the
  reverse) with a dot and a timestamp. These are factual events, not
  signals — present them as such, with no implied consequence.
- Overlay spot on a secondary axis for context (this is the one permitted
  dual-axis; note it in the UI as a context overlay).
- Requires a new store table (§5).

### 3.4 Strike gamma heatmap
Strikes (y) × poll time (x), colour = gamma. Sequential red ramp for
negative, blue for positive, neutral midpoint. Shows walls forming and
dissolving intraday.

### 3.5 Event markers
Alerts and shadow entries/exits from `alert_log` / `shadow_trade` drawn as
markers on the shared x-axis across all four panels, so "what did the bot
do and when" reads against the map that produced it.

---

## 4. Screen 3 — Research (`/research`)

Port the Streamlit explorer: bundle picker, headline stats, equity curve,
daily P&L, trade log, P&L by exit reason, §10 gates with the `max_dd`
threshold each run was judged against. Read-only from
`/api/backtests`. Retire `explore.py` once this is at parity.

---

## 5. New store table (Phase 1 addendum)

Premium drift needs per-poll cumulative totals; recomputing from raw prints
on every UI request would be wasteful and is not possible after a restart.

```sql
CREATE TABLE IF NOT EXISTS poll_premium (
    poll_id          BIGINT PRIMARY KEY,
    ts               TIMESTAMP NOT NULL,
    session_date     DATE NOT NULL,
    minute_of_day    INTEGER NOT NULL,
    call_bought      DOUBLE NOT NULL,   -- cumulative $ premium, session to date
    call_sold        DOUBLE NOT NULL,
    put_bought       DOUBLE NOT NULL,
    put_sold         DOUBLE NOT NULL,
    trades_counted   BIGINT NOT NULL,
    unclassified     BIGINT NOT NULL    -- tick rule returned 0; see §6
);
```

`FlowLedger` accumulates these alongside gamma; `write_poll` persists them.
Counters reset per session date.

API: `GET /api/session/{date}/premium` → rows above.

---

## 6. Honesty requirements in the UI

These are not decoration; each corresponds to a way we have already been
misled during this project.

1. **Classification confidence.** Tick-rule classification is roughly
   75–80% accurate versus NBBO. Wherever classified flow or premium is
   shown, a footnote states the method and links to the validation study
   result once it exists (queued: NBBO-vs-tick-rule comparison on the two
   full-quote days). Until that study runs, the footnote says the error is
   unquantified.
2. **`unclassified` count** is displayed on the premium panel. Zero-tick
   prints that inherit no prior direction are excluded from both sides; if
   that count is large, the panel is less meaningful and the user should
   see it.
3. **NULL is not zero.** Where the flow overlay was off, flow series render
   as gaps, never as zero lines.
4. **Mark provenance** (file / REST / model-fallback %) appears on the
   research screen for every backtest bundle, as it already does in the
   runner output.
5. **`as_of` everywhere.** Every panel shows the timestamp of the data it
   is drawing, not the time of render.

---

## 7. Build order

1. `ui/` scaffold, API client, WS hook, health strip. Prove live updates.
2. Screen 1 strike profile + right rail.
3. `poll_premium` table and API (§5) — small, unblocks 3.3.
4. Screen 2 panels, in order 3.1, 3.2, 3.4, 3.3, then 3.5 markers.
5. Screen 3, then delete `explore.py`.

Stop after each numbered step and show the result.

---

## 8. Acceptance

- With engine and API running, Screen 1 updates within ~2 s of a new poll
  without a reload.
- Killing the engine turns the header amber within 5 minutes and no panel
  shows stale numbers as if current.
- Screen 2 for 2026-09-11 renders from stored polls with no errors.
- Nothing under `gexbot/` implementing entries, exits, discipline, sizing,
  or gates is modified. `git diff --stat` confines Python changes to
  `state.py`, `livefeed.py` (premium counters), `watch.py`, `api/`.
- Tests remain green; new tests cover the premium accumulator including a
  case where buy and sell volumes differ (equal values cancel and would
  mask a broken net).

---

## 9. Out of scope

Auth, remote/Lightsail deployment, Docker, mobile layout, the design pass,
any greeks panel beyond gamma (vanna/charm exposure may come later, labelled
as exposure — never as a recommendation), and anything that forecasts.

---

## 10. Addendum — SPY and the S&P complex

The operator marks levels in SPY and trades SPX. The system currently sees
neither: `ReplayBuilder` filters to `("SPX","SPXW")`, `OptionsFeed`
subscribes only to `T.O:SPX*`/`T.O:SPXW*`, and `levels.py` fetches the
`I:SPX` chain. SPY options are downloaded in the historical archive and then
discarded.

Three distinct things follow. Build 10.1 and 10.2; keep 10.3 behind a flag.

### 10.1 SPY-scaled display of the SPX map

A display transform only: strikes, spot, and level labels divided by 10,
gamma values unchanged in dollars. Same underlying computation, presented in
the units the operator reads.

- UI: a `SPX / SPY units` toggle on the header strip. Persisted per user.
- Never mix: if units are SPY, every strike on screen is SPY-scaled,
  including the right rail, trend axes, and heatmap rows.
- This is NOT a SPY gamma map, and the UI must not imply it is. Label the
  toggle `units`, not `instrument`.

### 10.2 Standalone SPY GEX map

SPY's own chain, own open interest, own gamma profile. SPY walls sit at
genuinely different strikes than SPX walls — this is a second map, not a
rescaling of the first.

- `levels.py` already takes an `underlying`; verify it works with `"SPY"`
  (equity chains carry `underlying_asset.price`, so the index-spot fallback
  should not be needed) and that per-point units are right at SPY's price
  scale.
- `watch.py` gains `--underlying` plumbed through to the engine, and the
  live feed subscribes `T.O:SPY*` when SPY is selected.
- Storage: `poll_snapshot`, `alert_log` and `shadow_trade` gain an
  `underlying VARCHAR NOT NULL DEFAULT 'I:SPX'` column. Existing rows
  backfill to `I:SPX`. Every API session endpoint takes `?underlying=` and
  defaults to `I:SPX`.

  **Amended 2026-09-11, superseding the original wording**, which asked for
  the column on `poll_strike` too and for a composite primary key:

  - `poll_strike` does **not** carry `underlying`. It reaches one through
    `poll_id`. Denormalising would repeat the same string across ~155 rows
    per poll and create a second place for the two to disagree — the join is
    free and the single source of truth is worth more.
  - The primary key stays `poll_id` alone. Every poll of every instrument
    gets its own id from the same monotonic allocator, so `poll_id` is
    already globally unique; a composite key would be redundant, and it
    would wrongly imply that two instruments can share an id.
- UI: an **instrument selector** — `SPX · SPY · combined` — distinct from
  the units toggle in 10.1. Selecting SPY shows SPY's own map with its own
  levels.

### 10.3 Combined S&P complex ledger (flagged, measured)

Dealers hedge S&P exposure as one book, so SPX + SPXW + SPY gamma arguably
belong in one profile, with SPY contracts scaled to SPX notional (÷10 on
strike, gamma weighted accordingly).

This is a **model change**: it moves walls, shifts the flip, and can change
regime calls. Whether the combined map is more predictive than SPX-only is
an empirical question, and "more complete" is not self-evidently "better".

Therefore:

- Implemented behind `--complex` / instrument selector value `combined`,
  never as a default.
- When `combined` is selected, the store writes rows with
  `underlying = 'COMPLEX'` — a third series, not an overwrite. SPX-only
  and SPY-only maps continue to be computed and stored in parallel during
  any session where combined is enabled, so the comparison record
  accumulates automatically.
- No alerting from the combined map until the comparison has been reviewed.
  `watch.py` alerts continue to fire from `I:SPX`.
- Comparison question, to be answered from the accumulated record, not by
  eye: over N sessions, do combined-map levels coincide with turning points
  more often than SPX-only levels? Specify the test before reading the
  data, per `PREREGISTRATION.md` §2.

### 10.4 Overnight / pre-market summary

The operator manually checks which levels were touched after hours. The bot
cannot help today: `watch` sleeps outside 09:00–16:15 and OI is static
overnight anyway.

Build a pre-market job (~08:45 ET) posting to `#daily`:

- Overnight range, from a session-extended source (SPY extended hours via
  aggregates is sufficient; ES futures would need a Massive Futures
  subscription — do not add one for this).
- Which of the prior session's computed levels were touched or breached
  overnight, using the last stored `poll_strike` profile of that session.
- Where spot sits relative to that map at the open, in points and percent.

Strictly descriptive. No implication about what the touch means.

### 10.5 Cost

None. SPY options are OPRA, already covered by the Options Advanced
subscription; the chain snapshot endpoint is the same. No new entitlement,
no backfill — SPY trades are already on disk from 2021.
# Spec Addendum — Expiry Structure, Net Delta, and Narration

Appends to `docs/PHASE3_UI_SPEC.md`. Same constraints apply: `CLAUDE.md` §3
(strategy frozen), §0 of the UI spec (no directional claims, no forecasts,
no number we cannot source).

---

## 11. Gamma decay by expiry

Today's map is a snapshot with no sense of its own shelf life. A profile
where 57% of the gamma expires Friday is a different object from one where
it expires in a month, and nothing in the UI currently says which we have.

### 11.1 Computation

From the chain snapshot, group contracts by `expiration_date` and compute
dealer gamma per expiry using the same convention as `build_profile`
(per-point, same dealer model, same strike window).

For each future date `D` in the next ⚙30 calendar days:

```
remaining(D) = Σ gamma over contracts with expiry > D
pct(D)       = remaining(D) / remaining(today)
```

Store per poll:

```sql
CREATE TABLE IF NOT EXISTS poll_expiry (
    poll_id       BIGINT NOT NULL,
    expiry        DATE NOT NULL,
    gamma         DOUBLE NOT NULL,   -- dealer gamma expiring ON this date
    delta         DOUBLE NOT NULL,   -- dealer delta expiring ON this date (§12)
    oi            BIGINT NOT NULL,
    put_call_oi   DOUBLE,            -- put OI / call OI on this expiry
    PRIMARY KEY (poll_id, expiry)
);
```

`remaining` and `pct` are derived on read — do not store cumulative values
that can disagree with their components.

### 11.2 Display

A bar per upcoming expiry showing gamma expiring on that date, plus a line
showing percent of current gamma still alive after it. Mark known event
dates (monthly OPEX, quarterly OPEX) from an exchange calendar, not by
guessing from OI size.

State facts only: "57% of current gamma expires 9/18." Not "the structure
deteriorates into Friday."

### 11.3 Rolloff

Once two sessions of `poll_expiry` exist, the pre-market summary (§10.4)
reports what expired overnight: gamma removed, delta removed, and the
resulting change in net. Descriptive, sourced from two stored profiles.

---

## 12. Net delta (DEX)

`greeks.py` computes delta already; nothing aggregates it.

- Add `dex` (net dealer delta) and `dex_by_strike` alongside gamma in
  `build_profile`, same dealer-model convention.
- Store `net_dex` on `poll_snapshot`; per-strike delta on `poll_strike`.
- UI: a DEX toggle beside GEX on the strike profile, and a `net delta` field
  in the header strip. Same trend panel treatment as net gamma.
- Label it exposure. Never "mechanical bid," never "cushion" — those are
  interpretations of what delta exposure implies, and we have not tested
  whether the implication holds.

Week-over-week comparison of net gamma and net delta comes free once the
store has ≥2 weeks of sessions. Show it as a delta between two stored
values, with both dates named.

---

## 13. Narration (Anthropic API)

A readable description of the current map, generated from stored numbers.

### 13.1 What it is

`GET /api/session/narrate?underlying=&poll_id=` → a short paragraph
describing: where spot sits, net gamma and its sign, the nearest structure
above and below with distances, what the regime mechanically implies, what
changed since the previous poll, and the expiry picture from §11.

Posted once daily to `#daily` as a map summary; available on demand in the UI.

### 13.2 What it must not do

The system prompt forbids, and the endpoint rejects output containing:

- Directional language: bullish, bearish, upside, downside, rally, selloff,
  breakdown, breakout, target.
- Probability or likelihood of any price outcome.
- Trade suggestions, entry or exit levels, or sizing.
- Claims about what "will" or "should" happen.

Rationale, stated here so a future session does not relax it: this system's
entry logic was tested against four null models across two rounds and did
not beat random out-of-sample (`CLAUDE.md` §2). A language model handed the
same data will produce a fluent, confident directional read regardless,
because that is what its training distribution contains — and a prediction
generated from *your own* numbers feels more credible than a stranger's,
while having exactly the same (absent) validity. The model also cannot see
what it does not know: that tick-rule error is unquantified, that a given
poll's flow overlay was zero, that the 2022 control test failed.

### 13.3 Implementation

- Model: cheapest suitable tier. Input is the poll's stored fields, not raw
  chain data. Est. a few cents per day.
- Runs in its own process path; an API failure returns `narration: null`
  and the UI shows the numbers without prose. Never blocks a poll, never
  blocks an alert.
- Every narration is stored with its `poll_id` and the model version, so
  the text can be audited against the numbers that produced it.
- Output passes a lint against the forbidden-term list before storage or
  display. A violation is logged and the narration is dropped, not shown.

### 13.4 The predictive variant — logged, never displayed

If a directional narration is ever wanted, it is built as a blind
experiment, not a feature:

- A second prompt produces a directional call and stores it, with the poll
  it was generated from.
- It is **not shown in the UI or Discord.**
- After ≥⚙200 calls, score them against subsequent price movement and
  report the hit rate against a matched-random baseline — the same
  control-experiment discipline used for the strategy itself.
- Only a result that beats the baseline at p ≤ 0.05 earns display, and that
  decision requires a written pre-registration first.

---

## 14. Explicitly not building

From the reference posts that prompted this addendum:

- Rate-hike or event probabilities (not our data; sourced from futures
  markets we do not subscribe to).
- Regime labels like "G-D+" that compress gamma and delta signs into a
  taxonomy implying a behavioural forecast.
- Prose framings — "structure deteriorated", "thinner cushion", "wider
  ranges" — that assert consequence rather than state condition.
- MOC imbalance (needs an equity auction subscription; one inferential step
  from an SPX decision; no validated use).
- Dark pool prints (midpoint executions have no aggressor side, so
  direction is unknowable; needs a Stocks subscription).
# Spec §15 — Classified tape

Appends to `docs/PHASE3_UI_SPEC.md`. Constraints unchanged: `CLAUDE.md` §3
(strategy frozen), §0 (no directional claims, no forecasts, no unsourced
numbers).

---

## 15.1 What this is

A live feed of individual option prints — the classified trades already
flowing through `OptionsFeed` — displayed in sequence rather than
aggregated into the gamma profile.

The ledger answers "what is the dealer position now." The tape answers
"what just happened, in what order, at what size." Same input stream,
different processing. The aggregation is lossy by design; this is the
unaggregated view.

It is also a diagnostic. Today there is no way to see what the classifier
is doing — a tick rule that silently returns 0 for most prints, or a gamma
lookup returning 0 (the exact trap caught during the flow wiring), would
produce a flat overlay that looks like a quiet market. The tape makes that
visible immediately.

## 15.2 Data

Already arriving. `OptionsFeed._handle` parses ticker, price, size, and
computes a side; nothing retains the print.

Add a bounded in-memory ring buffer on the feed — ⚙2,000 most recent
classified prints, per instrument:

```
print: ts, ticker, root, expiry, strike, right, price, size,
       side (+1 buy / -1 sell / 0 unclassified),
       premium (price × size × 100),
       gamma_used (the value the lookup returned, 0 if missed),
       dealer_gamma_delta (the signed contribution written to the ledger)
```

`gamma_used` and `dealer_gamma_delta` are the diagnostic columns — they show
whether a print actually moved the map or was silently dropped.

**Not persisted to DuckDB by default.** SPX prints run to millions per
session; storing them duplicates the flat-file archive that already exists
and would dwarf every other table. A `--record-tape` flag may write a
session's prints to Parquet under `{GEX_DATA_ROOT}/tape/date=…` for a
specific investigation, off by default.

## 15.3 API

```
GET  /api/tape?underlying=&limit=&min_size=&strike=&right=&side=
       -> most recent prints, newest first, filtered
WS   /ws/live  -> new message type:
       { "type": "prints", "data": [ ...batch since last push... ] }
```

Batch prints on the existing 1 s push rather than streaming each one — a
per-print socket message on SPX would flood the browser.

## 15.4 Display

A panel on Screen 1, collapsed by default.

- Newest at top. Columns: time, strike, right, size, price, premium, side.
- Side shown by symbol and colour: `▲` buy, `▼` sell, `·` unclassified.
  Never colour alone — the unclassified case must be visually distinct, not
  merely absent.
- Rows at or beyond the ⚙2,000-contract block threshold (`C.12`) are
  emphasised; the same threshold the block-flow tracker uses, read from the
  API rather than duplicated in the UI.
- Filters: minimum size, strike, right, side.
- A header line shows, for the visible session: prints seen, contracts,
  **unclassified count and percentage**, and **gamma-lookup misses**. Those
  last two are the honesty numbers — if either is large, the flow overlay is
  less meaningful than it looks and the operator should see that on the same
  screen as the map.

## 15.5 What it must not do

- No inference. The panel shows prints. It does not label a sequence as
  "absorption", "aggression", "sweeps", or "institutional" — those are
  readings, and this project has no validated basis for any of them.
- No aggregate implying consequence — e.g. no "buy pressure" gauge. The
  premium panel (§3.3) already shows classified totals factually; that is
  the aggregate view.
- No sound, no flashing. A tape that demands attention competes with the
  alerts, which are the thing that actually earned the right to interrupt.

Context for §15.5, for future sessions: tape reading has a long folklore
tradition and thin published evidence. Order-flow imbalance does precede
short-term movement in the microstructure literature, but the retail
practice of forming impressions from scrolling prints is much weaker than
practitioners claim — and this system's own control tests found its
flow-derived entry logic did not beat random out-of-sample. Showing the
prints is useful; interpreting them on the operator's behalf is not
something we can currently justify.

## 15.6 Acceptance

- With the engine running during market hours, prints appear within ~2 s of
  the WebSocket receiving them.
- Unclassified percentage and gamma-miss count are visible without opening
  a menu.
- Memory is bounded: the ring buffer does not grow past its cap over a full
  session.
- Nothing under `gexbot/` implementing entries, exits, discipline, sizing,
  or contract selection is modified.
