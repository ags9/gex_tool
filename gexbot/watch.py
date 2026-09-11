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

from .config import settings
from .levels import BASE, build_profile, fetch_chain, fetch_index_spot
from .notify import Channel, Color, DiscordNotifier

console = Console()

SESSION_OPEN = 9 * 60 + 30
SESSION_CLOSE = 16 * 60
SHADOW_TAG = "📄 SHADOW"
DISCLAIMER = "_strategy failed validation — narration only, not advice_"


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
    last_side_of_flip: str = ""
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
    t = dt.datetime.now()
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
def regime_context(net: float) -> str:
    if net > 0:
        return ("Positive gamma: dealer hedging dampens moves — ranges tend "
                "to hold, breakouts tend to fail.")
    return ("Negative gamma: dealer hedging amplifies moves — levels break "
            "more easily and moves extend.")


def post_map(n: DiscordNotifier, prof: dict) -> None:
    """First poll of the session: today's structure, one card."""
    spot, net, flip = prof["spot"], prof["net"], prof.get("flip")
    rows = sorted(prof["by_strike"].items(), key=lambda kv: -abs(kv[1]))[:6]
    lines = [f"`{k:>7,.0f}`  {fmt_m(v):>8}" for k, v in
             sorted(rows, key=lambda kv: -kv[0])]
    n.send(Channel.ALERTS,
           f"Today's gamma map — SPX {spot:,.0f}",
           f"**Net GEX:** {fmt_m(net)}   "
           f"**Flip:** {f'{flip:,.0f}' if flip else 'none in window'}\n"
           f"**Put wall:** {prof.get('put_wall') or 0:,.0f}   "
           f"**Call wall:** {prof.get('call_wall') or 0:,.0f}\n"
           f"**Max accel:** {prof.get('max_accel') or 0:,.0f}   "
           f"**Max magnet:** {prof.get('max_magnet') or 0:,.0f}\n\n"
           "Largest gamma strikes:\n" + "\n".join(lines) + "\n\n"
           + regime_context(net),
           Color.BLUE)


def structural_alerts(n: DiscordNotifier, prof: dict, st: WatchState) -> None:
    spot, net, flip = prof["spot"], prof["net"], prof.get("flip")

    # regime flip
    regime = "POSITIVE" if net > 0 else "NEGATIVE"
    if st.last_regime and regime != st.last_regime:
        n.send(Channel.ALERTS, f"Regime flip → {regime} gamma",
               f"Net GEX {fmt_m(net)} (was {st.last_regime}).\n{regime_context(net)}",
               Color.AMBER,
               [("spot", f"{spot:,.0f}"), ("flip", f"{flip:,.0f}" if flip else "—")])
    st.last_regime = regime

    # flip crossing
    if flip:
        side = "above" if spot > flip else "below"
        if st.last_side_of_flip and side != st.last_side_of_flip:
            n.send(Channel.ALERTS, f"Spot crossed {side} the gamma flip",
                   f"Spot {spot:,.0f} is now {side} flip {flip:,.0f}.\n"
                   f"{regime_context(net)}", Color.AMBER)
        st.last_side_of_flip = side

    # proximity to significant strikes
    for label, k in (("put wall", prof.get("put_wall")),
                     ("call wall", prof.get("call_wall")),
                     ("max accelerator", prof.get("max_accel")),
                     ("max magnet", prof.get("max_magnet"))):
        if not k:
            continue
        dist = abs(spot - k) / spot
        key = f"{label}:{k:.0f}"
        if dist <= 0.002 and st.alerted_levels.get(key) != "near":
            gv = prof["by_strike"].get(k, 0.0)
            n.send(Channel.ALERTS, f"Spot at {label} {k:,.0f}",
                   f"Spot {spot:,.0f}, {dist*100:.2f}% away. "
                   f"Strike gamma {fmt_m(gv)}.\n{regime_context(net)}",
                   Color.BLUE)
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


def do_shadow(n: DiscordNotifier, prof: dict, st: WatchState, key: str,
              underlying: str, tranche: float) -> None:
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
        n.send(Channel.TRADES,
               f"{SHADOW_TAG} would BUY SPX {strike:,.0f}{'C' if direction>0 else 'P'} "
               f"exp {expiry:%-m/%-d/%Y}",
               f"**Trigger:** {p['trigger']}\n"
               f"**Contracts:** {contracts} @ ~${mid:.2f} (spread ${spread:.2f})\n"
               f"**Spot:** {spot:,.0f}  ·  **Net GEX:** {fmt_m(prof['net'])}\n"
               f"{regime_context(prof['net'])}\n\n{DISCLAIMER}",
               Color.BLUE)
    else:
        t = st.open_trade
        st.shadow_pnl += p["pnl"]
        t.update(closed_at=dt.datetime.now().isoformat(timespec="minutes"),
                 exit_premium=p["mid"], exit_reason=p["reason"], pnl=p["pnl"])
        st.closed_trades.append(t)
        st.open_trade = None
        n.send(Channel.TRADES,
               f"{SHADOW_TAG} would EXIT SPX {t['strike']:,.0f}"
               f"{'C' if t['direction']>0 else 'P'} — {p['reason']}",
               f"**P&L:** ${p['pnl']:+,.0f}  (entry ${t['entry_premium']:.2f} → "
               f"${p['mid']:.2f} × {t['contracts']})\n"
               f"**Spot:** {t['entry_spot']:,.0f} → {spot:,.0f}\n"
               f"**Shadow P&L today:** ${st.shadow_pnl:+,.0f}\n\n{DISCLAIMER}",
               Color.GREEN if p["pnl"] >= 0 else Color.RED)


# ── main loop ────────────────────────────────────────────────────────
def run_watch(*, underlying: str = "I:SPX", interval: int = 180,
              expiries: int = 2, window: float = 0.06,
              tranche: float = 3000.0, shadow: bool = True,
              once: bool = False) -> None:
    key = os.getenv("MASSIVE_API_KEY", "")
    if not key or key == "your_key_here":
        console.print("[red]MASSIVE_API_KEY not set")
        return

    n = DiscordNotifier({
        Channel.TRADES: os.getenv("DISCORD_WEBHOOK_TRADES", ""),
        Channel.ALERTS: os.getenv("DISCORD_WEBHOOK_ALERTS", ""),
        Channel.DAILY: os.getenv("DISCORD_WEBHOOK_DAILY", ""),
    }, paper_mode=False)          # shadow tagging is explicit in the text

    state_path = Path(settings.gex_data_root) / "watch_state.json"
    st = WatchState.load(state_path)
    today = dt.date.today().isoformat()
    if st.day != today:
        st = WatchState(day=today)

    console.print(f"[bold]watch started[/bold] · interval {interval}s · "
                  f"shadow {'on' if shadow else 'off'} · state {state_path}")

    while True:
        m = minute_now()
        if not once and not (SESSION_OPEN - 30 <= m <= SESSION_CLOSE + 5):
            time.sleep(60)
            continue
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
                if not st.posted_open_map:
                    post_map(n, prof)
                    st.posted_open_map = True
                structural_alerts(n, prof, st)
                if shadow:
                    do_shadow(n, prof, st, key, underlying, tranche)
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
