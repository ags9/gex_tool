"""`python -m gexbot watch` — market-hours monitor.

Two kinds of Discord output, deliberately separated:

  STRUCTURAL (#alerts)  factual observations about the gamma map, with a
                        mechanical context line. No trade implication.

  SHADOW (#trades)      what the strategy WOULD have done, entry AND exit,
                        with running P&L. Every message tagged as shadow,
                        because the strategy failed validation twice
                        (HANDOFF §2) and remembering only the good calls is
                        how false confidence forms.

Runs on REST chain snapshots (no WebSocket yet), so reaction time is
minutes and intraday flow is not in the ledger. Good enough for structure
and shadow narration; not the live engine.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

import httpx
from rich.console import Console

from .clock import ET
from .config import settings
from .levels import BASE, build_profile, fetch_chain, fetch_index_spot
from .notify import Channel, Color, DiscordNotifier
from .state import StateStore, default_path

console = Console()

SESSION_OPEN = 9 * 60 + 30
SESSION_CLOSE = 16 * 60
SHADOW_TAG = "📄 SHADOW"
DISCLAIMER = "_strategy failed validation — narration only, not advice_"


# ── alert sink ───────────────────────────────────────────────────────
class AlertSink:
    """Sends to Discord and records to the state store in one call.

    Spec §3.2 requires that the notifier and the store never disagree. The
    only way to guarantee that is to remove the opportunity: there is no path
    in this module that reaches `DiscordNotifier.send` directly, so an alert
    cannot be delivered without also being recorded, or recorded without
    being delivered.

    `kind` is keyword-only and required so every call site has to classify
    its message. A default would quietly file half the history under
    "warning" and make the table unqueryable.
    """

    def __init__(self, notifier: DiscordNotifier, store: StateStore):
        self.n = notifier
        self.store = store
        self.poll_id: int | None = None        # set by the loop each poll

    def send(self, channel: Channel, title: str, body: str,
             color: int = Color.BLUE, fields=None, *, kind: str,
             spot: float | None = None, strike: float | None = None) -> None:
        self.n.send(channel, title, body, color, fields)
        self.store.write_alert(channel=channel.value, kind=kind, title=title,
                               body=body, poll_id=self.poll_id, spot=spot,
                               strike=strike)

    def daily_digest(self, *, body: str, green_day: bool) -> None:
        self.n.daily_digest(body=body, green_day=green_day)
        self.store.write_alert(channel=Channel.DAILY.value, kind="digest",
                               title="Daily digest", body=body,
                               poll_id=self.poll_id)

    def flush(self, timeout: float = 15.0) -> None:
        self.n.flush(timeout)


# ── state ────────────────────────────────────────────────────────────
@dataclass
class ShadowTrade:
    opened_at: str
    direction: int                 # +1 call, -1 put
    strike: float
    expiry: str
    entry_spot: float
    entry_premium: float
    contracts: int
    trigger: str
    level: float
    peak_spot: float = 0.0
    closed_at: str | None = None
    exit_premium: float | None = None
    exit_reason: str | None = None
    pnl: float | None = None


@dataclass
class WatchState:
    day: str = ""
    last_regime: str = ""
    peak_abs_net: float = 0.0        # session's largest |net GEX|, sets the dead zone
    pending_regime: str = ""
    pending_regime_count: int = 0
    last_regime_alert_ts: float = 0.0
    last_side_of_flip: str = ""
    pending_flip_side: str = ""
    pending_flip_count: int = 0
    last_flip_alert_ts: float = 0.0
    alerted_levels: dict = field(default_factory=dict)
    posted_open_map: bool = False
    open_trade: dict | None = None
    closed_trades: list = field(default_factory=list)
    shadow_pnl: float = 0.0

    @classmethod
    def load(cls, p: Path) -> "WatchState":
        if p.exists():
            try:
                return cls(**json.loads(p.read_text()))
            except Exception:
                pass
        return cls()

    def save(self, p: Path) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(self), indent=2, default=str))


# ── helpers ──────────────────────────────────────────────────────────
def minute_now() -> int:
    """Exchange minute-of-day, via the clock module rather than the machine's
    local time. Every gate in this file — the alert window, the shadow entry
    cutoff, the 15:50 flat — is defined in ET; reading the wall clock instead
    silently shifts all of them on a box in another timezone."""
    t = dt.datetime.now(dt.timezone.utc).astimezone(ET)
    return t.hour * 60 + t.minute


def fmt_m(v: float) -> str:
    return f"{v/1e6:+,.0f}M"


def pick_expiry(days_out: int = 4) -> dt.date:
    """Nearest SPX expiry >= days_out (SPXW lists Mon/Wed/Fri)."""
    d = dt.date.today() + dt.timedelta(days=days_out)
    for _ in range(7):
        if d.weekday() in (0, 2, 4):
            return d
        d += dt.timedelta(days=1)
    return d


def contract_price(underlying: str, strike: float, right: str,
                   expiry: dt.date, key: str) -> tuple[float, float] | None:
    """Current mid/spread for one contract via snapshot."""
    occ = (f"O:SPXW{expiry:%y%m%d}{'C' if right == 'call' else 'P'}"
           f"{int(round(strike * 1000)):08d}")
    try:
        r = httpx.get(f"{BASE}/v3/snapshot/options/{underlying}/{occ}"
                      f"?apiKey={key}", timeout=20)
        if r.status_code != 200:
            return None
        res = (r.json() or {}).get("results") or {}
        q = res.get("last_quote") or {}
        bid, ask = q.get("bid"), q.get("ask")
        if bid and ask and ask >= bid > 0:
            return (bid + ask) / 2.0, ask - bid
        day = res.get("day") or {}
        if day.get("close"):
            return float(day["close"]), 0.5
    except httpx.HTTPError:
        return None
    return None


# ── alert composition ────────────────────────────────────────────────
FLIP_HYSTERESIS = 0.0015      # 0.15% band around the flip
FLIP_CONFIRM_POLLS = 2        # must hold for two consecutive polls
FLIP_COOLDOWN_S = 3600        # at most one flip alert per hour

# Regime calls get the same treatment as flip crossings, for the same reason:
# the sign of net GEX is meaningless when net GEX is near zero, and a book
# hovering around the boundary would otherwise announce a "regime flip" every
# few minutes. The dead zone is relative to the session's own largest reading
# — the same 5%-of-running-scale rule replay.py already uses to label regimes,
# so the live engine and the replay harness agree on what a regime IS, which
# is what makes signal-parity checks meaningful.
#
# The absolute floor exists because the relative rule is useless early, when
# the peak is itself tiny. ⚙ 25M is a judgement call on thin evidence: the one
# live poll recorded so far sat at net -12.3M with individual strikes at ±9M,
# i.e. aggregate noise — and it fired a regime-flip alert. Raise or lower it
# from the recorded history once there is some; it shapes alerts only and
# touches no entry, exit, sizing or discipline behaviour.
REGIME_DEAD_ZONE_FRAC = 0.05   # of the session's largest |net GEX| so far
REGIME_MIN_ABS_GEX = 25e6      # ⚙ absolute floor for calling a regime at all
REGIME_CONFIRM_POLLS = 2       # must hold for two consecutive polls
REGIME_COOLDOWN_S = 3600       # at most one regime alert per hour

# Structural alerts are silent outside these ET bounds. The chain snapshot
# still returns data after hours, so without this the engine narrates a
# stale book to a phone at 20:00.
ALERT_WINDOW_START = 9 * 60          # 09:00 ET
ALERT_WINDOW_END = 16 * 60 + 15      # 16:15 ET


def in_alert_window(minute: int | None = None) -> bool:
    m = minute_now() if minute is None else minute
    return ALERT_WINDOW_START <= m <= ALERT_WINDOW_END


def regime_of(net: float, peak_abs_net: float) -> str:
    """POSITIVE / NEGATIVE, or "" when |net| is too small to mean anything.

    "" is not a third regime — it is the absence of a call, and callers must
    treat it as "no information", never as a transition.
    """
    dead = max(REGIME_MIN_ABS_GEX, REGIME_DEAD_ZONE_FRAC * peak_abs_net)
    if abs(net) < dead:
        return ""
    return "POSITIVE" if net > 0 else "NEGATIVE"


def flip_context(side: str) -> str:
    """Regime implication of spot's position relative to the flip — NOT the
    net-GEX sign, which can disagree near the boundary."""
    if side == "above":
        return ("Above the flip: dealers net long gamma, hedging dampens "
                "moves — ranges tend to hold, breakouts tend to fail.")
    return ("Below the flip: dealers net short gamma, hedging amplifies "
            "moves — levels break more easily and moves extend.")


def regime_context(net: float) -> str:
    if net > 0:
        return ("Positive gamma: dealer hedging dampens moves — ranges tend "
                "to hold, breakouts tend to fail.")
    return ("Negative gamma: dealer hedging amplifies moves — levels break "
            "more easily and moves extend.")


def _level_lines(prof: dict) -> str:
    """Label levels by what hedging DOES there, not by convention."""
    from .levels import level_label
    bs = prof.get("by_strike") or {}
    spot = prof["spot"]
    out = []
    for key, below in (("put_wall", True), ("call_wall", False)):
        k = prof.get(key)
        if not k:
            continue
        g = bs.get(k, 0.0)
        lab = level_label(g, below)
        out.append(f"**{k:,.0f}** {fmt_m(g)} — {lab} "
                   f"({abs(spot-k)/spot*100:.1f}% {'below' if below else 'above'})")
    if prof.get("max_accel"):
        k = prof["max_accel"]
        out.append(f"**{k:,.0f}** {fmt_m(bs.get(k,0))} — MAX ACCELERATOR "
                   f"(hedging amplifies moves here)")
    if prof.get("max_magnet"):
        k = prof["max_magnet"]
        out.append(f"**{k:,.0f}** {fmt_m(bs.get(k,0))} — MAX MAGNET "
                   f"(hedging pins price here)")
    if prof.get("first_positive_above"):
        k = prof["first_positive_above"]
        out.append(f"**{k:,.0f}** — first positive gamma above spot "
                   f"({(k-spot)/spot*100:.1f}% away)")
    return "\n".join(out) + "\n\n"


def position_footer(prof: dict, st: "WatchState") -> str:
    """What the shadow book is holding — and if flat, the blocking reason.
    Appended to every structural alert so no message leaves you guessing."""
    spot, net = prof["spot"], prof["net"]
    t = st.open_trade
    if t:
        held = (f"{t['contracts']}× SPX {t['strike']:,.0f}"
                f"{'C' if t['direction'] > 0 else 'P'} exp {t['expiry']}")
        return (f"\n\n**📄 Shadow position:** {held} @ ${t['entry_premium']:.2f}"
                f"\nEntered {t['opened_at']} · spot then {t['entry_spot']:,.0f}"
                f" (now {spot:,.0f}) · trigger level {t['level']:,.0f}")

    # flat — say why no entry is possible right now
    m = minute_now()
    if net >= 0:
        why = f"net gamma positive ({fmt_m(net)}); breakout book needs negative"
    elif m < SESSION_OPEN + 15:
        why = "inside the opening 15 minutes"
    elif m > 14 * 60 + 30:
        why = "past the 14:30 entry cutoff"
    else:
        pw, cw = prof.get("put_wall"), prof.get("call_wall")
        if pw and cw:
            why = (f"spot inside the walls ({pw:,.0f}–{cw:,.0f}); "
                   f"entry needs a break beyond one")
        else:
            why = "no wall structure in the current window"
    done = ""
    if st.closed_trades:
        done = (f" · {len(st.closed_trades)} closed today, "
                f"shadow P&L ${st.shadow_pnl:+,.0f}")
    return f"\n\n**📄 Shadow position:** flat — {why}{done}"


def post_map(n: AlertSink, prof: dict, st: WatchState) -> None:
    """First poll of the session: today's structure, one card.

    Carries the position footer like every other structural alert — the
    footer's whole purpose is that no message leaves you guessing what the
    shadow book is holding.
    """
    spot, net, flip = prof["spot"], prof["net"], prof.get("flip")
    rows = sorted(prof["by_strike"].items(), key=lambda kv: -abs(kv[1]))[:6]
    lines = [f"`{k:>7,.0f}`  {fmt_m(v):>8}" for k, v in
             sorted(rows, key=lambda kv: -kv[0])]
    n.send(Channel.ALERTS,
           f"Today's gamma map — SPX {spot:,.0f}",
           f"**Net GEX:** {fmt_m(net)}   "
           f"**Flip:** {f'{flip:,.0f}' if flip else 'none in window'}\n"
+ _level_lines(prof) +
           "Largest gamma strikes:\n" + "\n".join(lines) + "\n\n"
           + regime_context(net)
           + position_footer(prof, st),
           Color.BLUE, kind="map", spot=spot)


def structural_alerts(n: AlertSink, prof: dict, st: WatchState) -> None:
    spot, net, flip = prof["spot"], prof["net"], prof.get("flip")

    # regime flip — dead zone, then persistence, then a cooldown. Without all
    # three a book sitting near net-zero announces a flip every few minutes,
    # and the sign it announces carries no information.
    st.peak_abs_net = max(st.peak_abs_net, abs(net))
    regime = regime_of(net, st.peak_abs_net)

    if not regime or regime == st.last_regime:
        # inside the dead zone, or nothing changed: forget any pending call
        st.pending_regime, st.pending_regime_count = "", 0
    elif not st.last_regime:
        # first real reading of the session: adopt it as the baseline, but do
        # not announce a "flip" from nothing
        st.last_regime = regime
        st.pending_regime, st.pending_regime_count = "", 0
    elif regime == st.pending_regime:
        st.pending_regime_count += 1
        if st.pending_regime_count >= REGIME_CONFIRM_POLLS:
            if time.time() - st.last_regime_alert_ts > REGIME_COOLDOWN_S:
                n.send(Channel.ALERTS, f"Regime flip → {regime} gamma",
                       f"Net GEX {fmt_m(net)} (was {st.last_regime}).\n"
                       f"Spot {spot:,.0f}"
                       + (f"  ·  flip {flip:,.0f}" if flip else "")
                       + f"\n{regime_context(net)}"
                       + position_footer(prof, st),
                       Color.AMBER, kind="regime_flip", spot=spot)
                st.last_regime_alert_ts = time.time()
            st.last_regime = regime
            st.pending_regime, st.pending_regime_count = "", 0
    else:
        st.pending_regime, st.pending_regime_count = regime, 1

    # flip crossing — needs distance (hysteresis), persistence, and a cooldown.
    # Without these the flip drifts with spot and the side oscillates on noise.
    if flip:
        band = flip * FLIP_HYSTERESIS          # ~0.15% ≈ 10 SPX points
        if spot > flip + band:
            side = "above"
        elif spot < flip - band:
            side = "below"
        else:
            side = ""                          # inside the dead zone: no call

        if not side or side == st.last_side_of_flip:
            st.pending_flip_side, st.pending_flip_count = "", 0
        elif side == st.pending_flip_side:
            st.pending_flip_count += 1
            if st.pending_flip_count >= FLIP_CONFIRM_POLLS:
                if time.time() - st.last_flip_alert_ts > FLIP_COOLDOWN_S:
                    n.send(Channel.ALERTS,
                           f"Spot crossed {side} the gamma flip",
                           f"Spot {spot:,.0f} vs flip {flip:,.0f} "
                           f"({abs(spot-flip)/flip*100:.2f}% {side}).\n"
                           f"{flip_context(side)}"
                           + position_footer(prof, st), Color.AMBER,
                           kind="flip_cross", spot=spot, strike=flip)
                    st.last_flip_alert_ts = time.time()
                st.last_side_of_flip = side
                st.pending_flip_side, st.pending_flip_count = "", 0
        else:
            st.pending_flip_side, st.pending_flip_count = side, 1

    # proximity to significant strikes — ONE alert per strike, labelled by
    # what hedging does there (gamma sign), not by convention.
    from .levels import level_label
    bs = prof["by_strike"]
    roles: dict[float, list[str]] = {}
    for role, k in (("largest below spot", prof.get("put_wall")),
                    ("largest above spot", prof.get("call_wall")),
                    ("max accelerator", prof.get("max_accel")),
                    ("max magnet", prof.get("max_magnet"))):
        if k:
            roles.setdefault(float(k), []).append(role)

    for k, role_list in roles.items():
        dist = abs(spot - k) / spot
        key = f"{k:.0f}"
        if dist <= 0.002 and st.alerted_levels.get(key) != "near":
            gv = bs.get(k, 0.0)
            below = k < spot
            lab = level_label(gv, below)
            pts = abs(spot - k)
            local = ("Negative gamma at this strike: hedging AMPLIFIES moves "
                     "through it." if gv < 0 else
                     "Positive gamma at this strike: hedging DAMPENS moves, "
                     "pinning price nearby.")
            mismatch = ""
            if (gv < 0) != (net < 0):
                mismatch = (f"\nNet GEX is {'positive' if net > 0 else 'negative'} "
                            f"overall ({fmt_m(net)}) — this strike is a local "
                            f"{'pocket of amplification' if gv < 0 else 'pocket of damping'}.")
            n.send(Channel.ALERTS,
                   f"Spot approaching {k:,.0f} — {lab}",
                   f"Spot {spot:,.0f}, {pts:.0f} pts "
                   f"{'above' if below else 'below'}. "
                   f"Strike gamma {fmt_m(gv)}  ·  {', '.join(role_list)}.\n"
                   f"{local}{mismatch}"
                   + position_footer(prof, st),
                   Color.BLUE, kind="proximity", spot=spot, strike=k)
            st.alerted_levels[key] = "near"
        elif dist > 0.004:
            st.alerted_levels.pop(key, None)


# ── shadow trading ───────────────────────────────────────────────────
def shadow_logic(prof: dict, st: WatchState, key: str,
                 underlying: str) -> tuple[str, dict] | None:
    """Breakout-book narration: confirmed move through a wall in negative
    gamma. Returns ('open'|'close', payload) or None."""
    spot, net = prof["spot"], prof["net"]
    minute = minute_now()

    # manage an open shadow trade
    if st.open_trade:
        t = st.open_trade
        right = "call" if t["direction"] > 0 else "put"
        px = contract_price(underlying, t["strike"], right,
                            dt.date.fromisoformat(t["expiry"]), key)
        if px:
            mid, _ = px
            t["peak_spot"] = (max(t["peak_spot"], spot) if t["direction"] > 0
                              else min(t["peak_spot"], spot))
            pnl = (mid - t["entry_premium"]) * 100 * t["contracts"]
            reason = None
            if minute >= 15 * 60 + 50:
                reason = "EOD flat (15:50)"
            elif (net > 0) != (t["direction"] < 0) and False:
                reason = None
            elif t["direction"] > 0 and spot < t["level"] - 5:
                reason = "level reclaimed against position"
            elif t["direction"] < 0 and spot > t["level"] + 5:
                reason = "level reclaimed against position"
            elif pnl <= -0.4 * t["entry_premium"] * 100 * t["contracts"]:
                reason = "stop (−40% premium)"
            if reason:
                return "close", {"mid": mid, "pnl": pnl, "reason": reason}
        return None

    # entry: negative gamma + spot beyond a wall
    if net >= 0 or minute < SESSION_OPEN + 15 or minute > 14 * 60 + 30:
        return None
    pw, cw = prof.get("put_wall"), prof.get("call_wall")
    if pw and spot < pw - 3:
        return "open", {"direction": -1, "level": pw, "trigger":
                        f"spot {spot:,.0f} below put wall {pw:,.0f} in negative gamma"}
    if cw and spot > cw + 3:
        return "open", {"direction": +1, "level": cw, "trigger":
                        f"spot {spot:,.0f} above call wall {cw:,.0f} in negative gamma"}
    return None


def do_shadow(n: AlertSink, prof: dict, st: WatchState, key: str,
              underlying: str, tranche: float,
              store: StateStore | None = None) -> None:
    act = shadow_logic(prof, st, key, underlying)
    if not act:
        return
    kind, p = act
    spot = prof["spot"]

    if kind == "open":
        direction = p["direction"]
        right = "call" if direction > 0 else "put"
        strike = round(spot / 5) * 5
        expiry = pick_expiry()
        px = contract_price(underlying, strike, right, expiry, key)
        if not px:
            return
        mid, spread = px
        contracts = max(1, int(0.25 * tranche / (mid * 100)))
        st.open_trade = asdict(ShadowTrade(
            opened_at=dt.datetime.now().isoformat(timespec="minutes"),
            direction=direction, strike=strike, expiry=expiry.isoformat(),
            entry_spot=spot, entry_premium=mid, contracts=contracts,
            trigger=p["trigger"], level=p["level"], peak_spot=spot))
        # carried in the JSON state so the exit can find its row after a
        # restart; None means the open write failed and the exit says so
        # rather than guessing at which row to close.
        st.open_trade["trade_id"] = store.open_shadow_trade(
            direction=direction, strike=strike, expiry=expiry,
            contracts=contracts, trigger=p["trigger"], level=p["level"],
            entry_spot=spot, entry_premium=mid) if store else None
        n.send(Channel.TRADES,
               f"{SHADOW_TAG} would BUY SPX {strike:,.0f}{'C' if direction>0 else 'P'} "
               f"exp {expiry:%-m/%-d/%Y}",
               f"**Trigger:** {p['trigger']}\n"
               f"**Contracts:** {contracts} @ ~${mid:.2f} (spread ${spread:.2f})\n"
               f"**Spot:** {spot:,.0f}  ·  **Net GEX:** {fmt_m(prof['net'])}\n"
               f"{regime_context(prof['net'])}\n\n{DISCLAIMER}",
               Color.BLUE, kind="shadow_entry", spot=spot, strike=strike)
    else:
        t = st.open_trade
        st.shadow_pnl += p["pnl"]
        t.update(closed_at=dt.datetime.now().isoformat(timespec="minutes"),
                 exit_premium=p["mid"], exit_reason=p["reason"], pnl=p["pnl"])
        if store:
            store.close_shadow_trade(t.get("trade_id"), exit_spot=spot,
                                     exit_premium=p["mid"],
                                     exit_reason=p["reason"], pnl=p["pnl"])
        st.closed_trades.append(t)
        st.open_trade = None
        n.send(Channel.TRADES,
               f"{SHADOW_TAG} would EXIT SPX {t['strike']:,.0f}"
               f"{'C' if t['direction']>0 else 'P'} — {p['reason']}",
               f"**P&L:** ${p['pnl']:+,.0f}  (entry ${t['entry_premium']:.2f} → "
               f"${p['mid']:.2f} × {t['contracts']})\n"
               f"**Spot:** {t['entry_spot']:,.0f} → {spot:,.0f}\n"
               f"**Shadow P&L today:** ${st.shadow_pnl:+,.0f}\n\n{DISCLAIMER}",
               Color.GREEN if p["pnl"] >= 0 else Color.RED,
               kind="shadow_exit", spot=spot, strike=t["strike"])


# ── main loop ────────────────────────────────────────────────────────
def run_watch(*, underlying: str = "I:SPX", interval: int = 180,
              expiries: int = 2, window: float = 0.06,
              tranche: float = 3000.0, shadow: bool = True,
              once: bool = False, state_db: Path | None = None) -> None:
    key = os.getenv("MASSIVE_API_KEY", "")
    if not key or key == "your_key_here":
        console.print("[red]MASSIVE_API_KEY not set")
        return

    notifier = DiscordNotifier({
        Channel.TRADES: os.getenv("DISCORD_WEBHOOK_TRADES", ""),
        Channel.ALERTS: os.getenv("DISCORD_WEBHOOK_ALERTS", ""),
        Channel.DAILY: os.getenv("DISCORD_WEBHOOK_DAILY", ""),
    }, paper_mode=False)          # shadow tagging is explicit in the text

    store = StateStore(state_db or default_path())
    n = AlertSink(notifier, store)

    state_path = Path(settings.gex_data_root) / "watch_state.json"
    st = WatchState.load(state_path)
    today = dt.date.today().isoformat()
    if st.day != today:
        st = WatchState(day=today)

    console.print(f"[bold]watch started[/bold] · interval {interval}s · "
                  f"shadow {'on' if shadow else 'off'} · state {state_path}")
    console.print(f"[bold]state store[/bold] · {store.path}"
                  + ("" if store.available else " [red](unavailable — "
                     "polls will not be recorded; alerting is unaffected)"))

    while True:
        m = minute_now()
        if not once and not (SESSION_OPEN - 30 <= m <= SESSION_CLOSE + 5):
            time.sleep(60)
            continue
        t0 = time.monotonic()
        try:
            spot = fetch_index_spot(underlying, key)
            exp_cap = (dt.date.today() + dt.timedelta(days=14)).isoformat()
            contracts, csp = fetch_chain(underlying, key, spot_hint=spot,
                                         strike_window=window,
                                         expiry_before=exp_cap)
            spot = csp or spot
            prof = build_profile(contracts, spot, model="naive",
                                 per_point=True, expiries=expiries,
                                 strike_window=window)
            if prof:
                console.log(f"spot {prof['spot']:,.0f} net {fmt_m(prof['net'])} "
                            f"flip {prof.get('flip') or float('nan'):,.0f}")
                # record the poll BEFORE any alert fires, so every alert this
                # poll produces can point at the map that produced it
                n.poll_id = store.write_poll(
                    prof, poll_ms=int((time.monotonic() - t0) * 1000),
                    model="naive", per_point=True)
                # Structural narration is silent outside 09:00-16:15 ET. The
                # poll itself is still recorded — the history should not have
                # holes just because nobody wanted a phone notification — and
                # the shadow book keeps its own, tighter time gates.
                if in_alert_window(m):
                    if not st.posted_open_map:
                        post_map(n, prof, st)
                        st.posted_open_map = True
                    structural_alerts(n, prof, st)
                if shadow:
                    do_shadow(n, prof, st, key, underlying, tranche, store)
                st.save(state_path)
        except Exception as e:                       # never die on a poll
            console.log(f"[red]poll error: {e}")

        if once:
            break
        # end-of-day digest
        if m >= 16 * 60 and st.closed_trades:
            wins = sum(1 for t in st.closed_trades if (t.get("pnl") or 0) > 0)
            n.daily_digest(
                body=(f"{SHADOW_TAG} shadow summary\n"
                      f"Trades: {len(st.closed_trades)} ({wins} green)\n"
                      f"Shadow P&L: ${st.shadow_pnl:+,.0f}\n\n{DISCLAIMER}"),
                green_day=st.shadow_pnl >= 0)
            st.closed_trades = []
            st.save(state_path)
        time.sleep(interval)
    n.flush()
