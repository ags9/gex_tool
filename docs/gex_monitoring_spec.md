# GEX Strategy — Monitoring, Dashboard & Alerting Spec v0.1
(Addendum to gex_strategy_spec.md — section numbers continue from it)

## 12. Architecture

The trading engine exposes state; the dashboard and alerter consume it.
Nothing in the monitoring path can block or crash the trading loop.

```
trading engine ──► state store (SQLite/DuckDB + in-memory snapshot)
                     ├──► FastAPI backend ──► browser dashboard (localhost)
                     └──► alert dispatcher ──► Discord webhooks (3 channels)
```

- State store: every §11 log record lands here. DuckDB recommended — same
  file serves live dashboard queries AND later research/backtest analysis.
- Dashboard: FastAPI + WebSocket push to a single-page frontend, served on
  localhost only (never expose the port; it sits next to trading credentials).
  Alternative if you want zero frontend work: Streamlit with 1s auto-refresh.
- Alert dispatcher: async queue; trading loop enqueues, dispatcher POSTs.
  Rate-limit to ≤ 25 msgs/min/channel (Discord limit ~30); coalesce bursts.

## 13. Discord Channels & Alert Catalog

| Channel | Ping | Events |
|---|---|---|
| `#trades` | no | Entry submitted / filled (strategy, strikes, width, contracts, credit-or-debit, intended vs filled price); exit filled (reason: target/stop/regime/time, P&L); order rejected |
| `#alerts` | @you | Any circuit breaker (§8) with trigger value; halt/re-arm; feed staleness >5s; Schwab auth failure; token expiry T-24h warning; signal-parity mismatch in paper mode; regime change (P↔N) — regime only, no ping |
| `#daily` | no | 16:15 ET digest: trades, win rate, gross/net P&L, fees paid, slippage total, equity vs HWM, breaker status; Sunday weekly rollup |

Message format: one embed per event, color-coded (green fill/profit, red
stop/breaker, amber warnings, blue info). Every embed carries the trade_id
so Discord history cross-references the state store.

## 14. Dashboard Pages

### 14.1 Live Ops (default view)
- Header status lights: engine running / halted (+reason), Schwab connected,
  Massive options feed, Massive indices feed, last-tick age per feed.
- Regime banner: REGIME_P / N / X, time in regime, GEX_net with sparkline.
- Level map: horizontal price ladder — SPOT now vs GEX_flip, G_MAX,
  PUT_WALL, CALL_WALL; entry-zone shading per §3/§4.
- Open positions table: strategy, legs, contracts, entry, current mark,
  unrealized P&L, distance to target and stop (as progress bars), time to
  forced exit.
- Account strip: EQ, HWM, drawdown %, open risk vs 4% cap, risk_per_trade
  currently computed, today's realized P&L vs −2% daily breaker (gauge).

### 14.2 Exposure Ledgers
- GEX by strike (bar chart, put/call split), intraday flow-adjusted vs
  OI-baseline overlay — this visualizes what the flow classifier is adding.
- VEX and CHARM by strike; net values with day sparkline.
- IVR, VRP, VIX_TS tiles with 20-day history strips.
- Classified tape: last N SPX/XSP trades with side, size, strike, and the
  gamma each added/removed (sanity-checks the Lee–Ready classifier live).

### 14.3 Performance
- Equity curve (live + paper phases marked), drawdown chart.
- Distribution of trade P&L; win rate, profit factor, avg win/avg loss —
  split by strategy (A vs B) and by exit reason.
- Slippage ledger: intended vs filled, cumulative cost; fees paid vs the
  $298/mo data + commission hurdle (the §10.4 gate, tracked live).
- Filter effectiveness: signals fired vs blocked, by blocking filter —
  tells you which rules are earning their place.

### 14.4 System / Audit
- Circuit-breaker panel: each §8 breaker, current value vs threshold, armed
  state; manual halt button (halting is one click; RE-ARMING is not — config
  file edit only, per §8).
- Config viewer: active parameter file version + diff vs previous (§9 audit).
- Log tail with severity filter; alert dispatch history.

## 15. Non-negotiables

1. Read-only by design: the dashboard renders state; the ONLY control it
   exposes is HALT. No entries, no parameter edits, no re-arm from the UI —
   §9's change-control friction stays intact.
2. Monitoring failure ≠ trading failure: dashboard or Discord down must not
   affect the engine (separate process; queue with drop-oldest).
3. Everything on the dashboard comes from the same state store the backtest
   analyzer reads — one source of truth, no dashboard-only calculations.
4. Localhost only. If remote viewing is wanted later, tunnel via Tailscale
   or SSH — never a port-forward of a box holding brokerage credentials.

---

## 18. Nightly Trade Analysis (evidence generator — NOT a tuner)

Runs after the session's flat files publish. Computes diagnostics per trade
and per day, writes them to the state store, posts a digest to `#daily`.

### 18.1 Structural rule (non-negotiable)

The analysis module has **no write access to the parameter config**. It
cannot propose-and-apply, cannot A/B itself into a new setting, cannot
"learn" between sessions. Enforced the same way block flow is: no code path
exists, and a test asserts it.

Rationale, learned expensively in round one: a strategy tuned on the data it
was evaluated on beat four null models at p<0.05 and had **zero edge** on
data it had not seen. Nightly self-tuning is that failure mode running at
maximum frequency on minimum sample. In any 3-trade day, *something* would
have worked better — fitting that is noise-chasing with a progress bar.

### 18.2 Per-trade diagnostics

| Metric | Definition | Why |
|---|---|---|
| MAE | max adverse excursion (spot & premium) before the exit | how much heat was taken |
| MFE | max favourable excursion after entry | was the exit early? |
| MFE-after-exit | best price available in the ⚙60 min AFTER exit | quantifies left-on-table |
| entry_quality | distance from the level that triggered it, in ATR units | did we chase? |
| exit_counterfactuals | P&L had each OTHER exit rule fired instead | isolates exit choice |
| slippage | intended vs filled, both legs | execution cost tracking |
| regime_at_entry / at_exit | ledger state | regime attribution |
| block_flow_context | C.12 scores in the ⚙3 bars before entry | question-8 dataset |

### 18.3 Per-day diagnostics

- Signals fired vs blocked, by blocking filter, with the P&L the blocked
  trades **would have** produced (filter effectiveness — §14.3).
- Regime distribution (minutes in P/N/X) and number of flips.
- Mark provenance (file / REST / model-fallback %) — credibility stamp on
  every day's numbers.
- Live-vs-replay signal parity diff (paper phase): same day, same decisions?

### 18.4 The bias warning that ships with it

MFE-after-exit is **systematically misleading** on its own: a trade that
exited at +30% and then ran to +80% looks like an error only because it
ran. Counting those as mistakes biases every review toward looser exits —
which the round-one sweeps showed leads to no exits at all. Therefore:

- MFE-after-exit is reported **paired with** its opposite: trades where
  holding longer would have given back profit or turned a winner into a
  loser. Both columns, always, or neither.
- No single day's diagnostics may motivate a change (§9: ≥⚙40 trades).

### 18.5 The human loop (where change actually happens)

1. Nightly: diagnostics accumulate. Digest to `#daily`. No decisions.
2. Weekly: a summary file is generated for review — the same artifact
   shape the backtest bundle uses, so it drops straight into a session.
3. Change proposals must: cite ≥⚙40 trades, name a pre-registered
   question, and pass a control re-test before adoption (prereg §2.3).
4. Adopted changes edit the versioned config by hand, git-committed with
   rationale. The bot reads the file; it never writes it.

### 18.6 What "improve" legitimately means here

Not parameter drift. The system improves by: (a) accumulating evidence that
answers pre-registered questions, (b) retiring mechanisms shown to be inert,
(c) surfacing execution problems (slippage, fill quality, feed gaps) which
ARE safe to fix immediately since they are engineering, not strategy.
