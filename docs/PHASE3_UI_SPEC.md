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
