# Exit Manager — Spec

**For:** Claude Code, against `/Volumes/Main Drive/development/GexDev`
**Read first:** `CLAUDE.md`. Everything there still binds.

---

## 0. What this is, and why it is small

The operator's losing trades have one cause, stated by him: he was pulled
into a work call and could not exit, or he overrode his own read. Neither is
a signal problem. Both are presence problems.

So this is not an autonomous trading system. **The human enters. The bot
owns the exit.** No entry logic, no strategy, no directional decisions.

That distinction is what makes this buildable now. Autonomous entry requires
a validated edge, and two rounds of testing failed to produce one
(`CLAUDE.md` §2). Exit management requires no such proof: a rule that closes
a position when the operator is unreachable is better than no rule.

**Resist scope growth.** Every feature beyond this needs an answer to "how
do we know it helps," and this project's history is that our answers were
twice wrong. If a future session is tempted to add entry signals here,
the answer is no — that goes through `PREREGISTRATION.md`.

---

## 1. Workflow

1. **Morning, at the desk.** Operator opens the chain view, sees today's GEX
   levels, forms his own view.
2. **Any time, from anywhere.** He enters a position — ideally in three taps
   on a phone. He picks contract and size; nothing else is asked.
3. **On fill.** The bot takes ownership and applies §2. He puts the phone
   away.
4. **On exit.** Discord message: what closed, why, at what price, P&L.

---

## 2. The four exit rules

Evaluated in this order on every tick; first match wins.

### 2.1 Level target
Close when spot reaches the GEX level the trade was aimed at.

The operator selects that level at entry (default: nearest significant level
in the direction of the position). This mirrors what he actually does — on
2026-09-11 he entered on a rejection at SPY 766 and exited at 765 because
765 was the magnet. A percentage target is a worse approximation of the same
decision.

- Levels come from the engine's own computation, never reimplemented.
- Reaching the level means spot trading at or through it, not merely
  approaching. ⚙ A small buffer (1-2 SPX points) avoids exiting on a tick.

### 2.2 Time stop
Close after ⚙30 minutes if the level target has not been hit.

His winners work in about ten minutes. A trade still open at thirty is not
the trade he took. This is the rule most likely to have saved the losing
days, and it is the one a distracted human cannot enforce.

### 2.3 Hard flat
Close unconditionally at ⚙15:50 ET. He exits same-day regardless; this makes
that true even when he is unreachable. Overnight exposure on a position
entered as an intraday trade is a different trade.

### 2.4 Disaster backstop
A resting stop order at the broker, set **wide** (⚙ −50% of premium), placed
on entry and cancelled on normal exit.

This exists only for the case where the bot, the Mac, or the connection
dies. It is not the risk management — option stop orders fill badly, because
option books gap. The real exits are 2.1–2.3, managed by the bot against
live quotes.

If the backstop ever fills while the bot believes the position is open:
halt, alert, and do not enter anything else until a human clears it.

---

## 3. Shadow mode first

Before the bot is allowed to close anything, it runs in shadow for ⚙3 weeks.

- Operator enters and exits manually, as today.
- The bot watches the position and records what it *would* have done: which
  rule would have fired, when, at what price, and the P&L that would have
  resulted.
- Discord and the UI show both: his actual exit, and the bot's counterfactual.

At the end, one question decided from the record and not from memory: **were
the bot's exits better or worse than his?** Report win rate, average P&L,
and — the number that matters most — what happened on days he was
unavailable. The bot does not need to beat him on his best days. It needs to
beat him on the days he was in a meeting.

Promotion to live management requires him to look at that record and decide.
No automatic promotion.

---

## 4. Entry surface

Deliberately minimal. This is not a trading terminal.

- **Chain view**: SPX, ATM ± ⚙5 strikes, ⚙3 DTE default, showing strike,
  bid/ask, mid, and the GEX at that strike. Today's levels shown alongside.
- **Order ticket**: contract, size, and the target level (pre-filled with
  the nearest significant level in that direction). Everything else is
  preset. One confirm.
- **Presets** set once, editable at the desk: default DTE, default size,
  time stop, hard-flat time, backstop width.
- **Position card**: what is open, entry, current mark, P&L, which rule is
  closest to firing and how far away.

No chart. The operator uses TradingView and is used to it.

---

## 5. Access from the road

The operator is at client offices. `127.0.0.1` is useless there.

- **Tailscale.** Private network, no public exposure, ~30 minutes to set up.
  This is the recommended path and should be the default.
- **Not** a public Lightsail deployment for v1. A box that can move money
  and is reachable from the internet needs auth, TLS, rate limiting, and a
  hardening review. That is a separate piece of work with a separate spec.
- The phone view is the primary view. Design the entry surface for a
  phone first; the desktop layout is the easy case.

### 5.1 The Schwab re-auth problem
Refresh tokens expire every 7 days and renewal requires a browser login. So
the system will be unusable once a week until the operator is at a browser.

- Alert in `#alerts` at T-24h and T-6h before expiry.
- The UI shows auth state prominently. An expired token must not present as
  a working entry screen.
- Do not attempt to automate the browser flow.

---

## 6. What it must never do

- **Enter a position.** Ever. No signal, no suggestion-with-a-button, no
  "the setup you traded last Tuesday is back."
- **Re-enter after an exit.** The exit ends the bot's involvement.
- **Average down, roll, or adjust.** It closes; it does not manage into
  something else.
- **Hold overnight.** §2.3 is unconditional.
- **Override a manual exit.** If the operator closes it himself, the bot
  stands down immediately and cancels the backstop.

---

## 7. Failure handling

The operator is unreachable by assumption, so silent failure is the worst
case.

- Feed stale > ⚙60s with a position open → alert, and fall back to the
  broker's quote for exit evaluation rather than acting on stale data.
- Schwab order rejected → retry ⚙twice, then alert loudly. Never silently
  abandon an exit.
- Bot restarts with a position open → recover it from broker state, not
  from local memory; reconcile against the last known state and alert on
  any mismatch.
- If the bot cannot confirm it knows the true position, it alerts and does
  nothing rather than guessing.

---

## 8. Acceptance

- Shadow mode records a counterfactual exit for every manually entered
  position, with the rule that fired and the price.
- A position entered on the phone view is managed without further input.
- Killing the engine mid-position produces an alert, and on restart the
  position is recovered from the broker and reconciled.
- Hard flat fires at the configured time in testing, with a simulated
  position, without human input.
- Nothing under `gexbot/` implementing entries, discipline, sizing, or
  gates is modified. This is new code plus the Schwab gateway.

---

## 9. Out of scope

Entry signals, autonomous trading, LEAPs, multi-leg, non-SPX instruments,
public hosting, and anything that decides *what* to trade rather than *when
to close it*.
