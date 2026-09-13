# Round Four Pre-Registration — 0DTE SPX Iron Condors

**Supersedes nothing.** `PREREGISTRATION.md` (v1) and
`PREREGISTRATION_V2.md` governed rounds one to three; all three hypotheses
failed and v2 §7 fired. This registers a **different strategy family** and
starts a **new holdout**. Commit and tag `prereg-v3` before any analysis.

Freeze date: ______________ · Tag: `prereg-v3` · Author: ags9

---

## 0. Why this is not a fourth attempt at the same thing

v2 §7 abandoned the *dealer-gamma entry* family after three failures. This
does not reopen it. Nothing here uses GEX levels to decide entry, direction,
or timing.

| Round | Family | Result |
|---|---|---|
| 1–3 | Directional entry from gamma levels | Failed. Round three: strategy −$87/trade vs matched-random −$46 |
| 4 (this) | Selling defined-risk premium on a clock | Untested |

The mechanism is different in kind. Rounds one to three bet on **direction**
inferred from structure. This bets on the **variance risk premium** — the
persistent, documented tendency of implied volatility to exceed subsequent
realised volatility, which makes short option positions positive-expectancy
on average. That premium is a measured property of the options market, not
an inference about where price is going.

It is also, notably, the thing that actually works for most retail
automation: mechanical, simple, no signal extraction. The sophistication in
rounds one to three went into finding a signal, which is where the edge
wasn't.

**What this does not assume:** that the premium is large enough to survive
costs, that 0DTE still carries it after the post-2022 crowding, or that the
tail is survivable. Those are what the test is for.

## 1. Capital

$25,000 tranche. This is a change from prior rounds and it is load-bearing:
at $3,000 the strategy is untradeable regardless of edge, because SPX
spreads risk ~$500 each and XSP's book (measured: 5–6 wide a few strikes
out, against SPX's 0.45) costs more in crossing than the trade collects
across four legs.

**SPX only.** XSP is excluded for that reason, and the exclusion is
registered rather than left as a preference.

## 2. Hypotheses, stated before analysis

**H1 (primary).** A mechanically-entered, defined-risk short-premium
position on SPX 0DTE has positive expectancy after realistic costs.

**H2 (null comparison).** That expectancy is not reproduced by matched
random entries — same times, same structures, same trade counts.

**H3 (tail).** The loss distribution is survivable at the registered
sizing: no single trade exceeds the spread width, and no single day exceeds
⚙ −5% of tranche.

**Explicitly not hypotheses:**
- That 10:30 is the right entry time (§3.1 states why it is the default and
  lists it as sweepable).
- That any particular delta, profit target, or stop is correct.
- Anything involving GEX levels. They may be *recorded* as context for a
  later question; they may not gate entry, exit, or sizing in this round.

## 3. The registered strategy

### 3.1 Entry
- **Structure:** iron condor — short put spread and short call spread, both
  defined risk.
- **Time:** ⚙10:30 ET, one entry per session, no re-entry.
  Rationale, stated so it is not mistaken for an optimum: the first hour is
  the noisiest part of the session (overnight unwind, opening auction, peak
  vol), so a delta-selected strike chosen at 10:30 is measured against a
  more stable range than the 09:30 print. The counter-argument is real —
  entering earlier collects more remaining theta — which is why entry time
  is sweepable after Stage 1, never before.
  **Caution for the sweep:** a fixed clock time is the parameter most
  likely to produce a spurious result, because genuine intraday seasonality
  can be real in-sample and unstable out. A sharp peak at one time with
  nothing nearby is noise, not a finding.
- **Strikes:** ⚙16-delta short strikes on each side, ⚙20-point wings.
  Delta-based selection self-adjusts to vol; fixed widths do not.
- **DTE:** 0. Same-day expiry, SPXW.
- **Liquidity gate:** skip the session if either short leg's quoted spread
  exceeds ⚙$1.00, or if any leg is unquotable.

### 3.2 Exit
- **Profit target:** close at ⚙50% of credit received.
- **Stop:** close at ⚙2× credit received (net loss ≈ 1× credit).
- **Time:** close everything at ⚙15:45 ET. 0DTE, so expiry is not an exit —
  holding to settlement is a different trade with different risk and is not
  registered here.
- One position at a time. No adjustment, no rolling, no adding.

### 3.3 Sizing
- ⚙1% of tranche at risk per trade, computed from actual max loss
  (width − credit) × 100 × contracts.
- Max ⚙3 contracts, hard cap.
- No scaling with recent results, no martingale, no size changes inside a
  session.

### 3.4 Event handling
No entry on FOMC decision days before ⚙14:30, or on CPI/NFP release
mornings before ⚙11:00. Scheduled-event vol behaves differently from the
premium being harvested; excluding those days is registered up front rather
than discovered afterwards.

## 4. Data partition (FROZEN)

| Partition | Dates | Use |
|---|---|---|
| Development | 2022-01-03 → 2025-12-31 | Unlimited exploration |
| Checkpoint | 2026-01-02 → freeze date | ONE run at the end |
| True holdout | Sessions after the freeze date | Shadow/paper. The only clean test |

2021 excluded (quote coverage too thin). 0DTE SPX did not have daily
expirations for the whole period — **the harness must verify same-day
expiry availability per session and skip days without it**, reporting the
count. A silently reduced sample is the failure mode here.

## 5. Method order (FROZEN)

1. Harness: multi-leg construction, per-leg fills, delta-based strike
   selection from real chains. Known-answer tests before any run.
2. **Control experiment first**, registered defaults, four null arms,
   ≥20 seeds.
3. Tuning only if Stage 1 passes. ≤⚙6 parameters total.
4. Control re-run after tuning.
5. Checkpoint. Once.
6. Shadow on live sessions.

## 6. Criteria (FROZEN)

**Stage 1 — signal.** P(random ≥ strategy) ≤ 0.05 on all four null arms.

Note on the null arms for a short-premium strategy: matched-random must
**sell the same structures at random times**, not take random directional
positions. A random condor still collects the variance premium — which is
the point. If the strategy cannot beat randomly-timed condors, then entry
timing adds nothing and the honest conclusion is "sell condors at any time",
not "this strategy works".

`shuffled_levels` is not applicable (no levels are used). Report three arms;
record the fourth as n/a. **This is a deliberate departure from v2 and is
registered, not discovered.**

**Stage 2 — after tuning, development set:**
- ≥300 IS trades, ≥100 OOS trades (walk-forward within development)
- OOS profit factor ≥ 1.3
- OOS max drawdown ≤ 20% of tranche
- Annualised P&L ≥ ⚙$5,000 at $25k (20%; below this the strategy does not
  justify the attention or the tail risk)
- PF excluding top 5 trades ≥ 1.15
- **Tail, reported first:** worst single trade ≤ registered max loss; worst
  day ≤ ⚙5% of tranche; ≤⚙2% of trades hitting max loss

**Stage 3 — checkpoint (2026).** PF ≥ 1.2, positive net, and the tail
criteria hold. A miss returns it to development **with a new holdout**.

**Stage 4 — shadow.** ≥20 sessions, ≥30 trades, signal parity clean.

## 7. Anti-fooling rules

Carried forward, plus two specific to this family:

- **Win rate is never a headline.** Short premium produces high win rates
  by construction. Every report leads with the loss distribution and the
  worst day. A 90% win rate with a −$4,000 tail is not a good strategy.
- **No "adjustment" mechanics.** Rolling, widening, or adding to a losing
  condor converts a defined-risk trade into an open-ended one and inflates
  the win rate by deferring losses. It is excluded by §3.2 and may not be
  introduced during tuning.
- Every run labelled `development` or `evidence` in `RUNLOG.md` at launch.
- Mark provenance printed on every run; a bundle above ⚙25% model fallback
  is refused, not warned about.
- Known-answer tests, hand-computed to the cent, before any result is read.
  Nine defects surfaced in round three, six pre-existing; three of them —
  one-sided commission, unfloored fills, unattached mark fetcher — were
  found only by arithmetic that had to match exactly.

## 8. Kill criteria

Abandoned, not retried, if:
- Stage 1 fails on the development set, OR
- Stage 2 needs more than ⚙6 tuned parameters, OR
- Stage 3 fails after a Stage 2 pass, OR
- The tail criteria in §6 fail at any stage, even with strong headline P&L.

That last clause is the one most likely to bind, and it is registered
deliberately. A short-premium strategy that makes money for months and then
gives it all back in one session has not worked; it has postponed.

If this fails, the conclusion is that this operator's automated edge is in
**execution and risk management**, not entry — and the project's remaining
value is the observability platform and the exit manager.
