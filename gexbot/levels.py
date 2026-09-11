"""Live GEX map — `python -m gexbot levels`

Pulls the full SPX option chain snapshot (open interest + greeks + IV per
contract) and computes the dealer gamma profile: net GEX, flip point,
walls, magnets, and the accelerator chain below spot.

This is the OI-based methodology the public GEX accounts use, so the output
is directly comparable to their nightly maps. Flow adjustment (our intraday
edge) can be layered on later; this is the baseline.

Convention (--model):
  naive      dealers long calls / short puts   (the classic public convention)
  short_all  dealers short all customer OI     (customers net long options)
"""
from __future__ import annotations

import datetime as dt
import os
from collections import defaultdict

import httpx
from rich.console import Console
from rich.table import Table

BASE = "https://api.polygon.io"
console = Console()


def fetch_chain(underlying: str, api_key: str, *,
                max_pages: int = 30, limit: int = 250,
                strike_window: float = 0.10,
                spot_hint: float = 0.0,
                expiry_before: str = "") -> tuple[list[dict], float]:
    """Full chain snapshot. Returns (contracts, spot)."""
    # server-side filters collapse a ~30-page walk into 1-3 pages
    flt = ""
    if spot_hint:
        lo = int(spot_hint * (1 - strike_window))
        hi = int(spot_hint * (1 + strike_window))
        flt += f"&strike_price.gte={lo}&strike_price.lte={hi}"
    if expiry_before:
        flt += f"&expiration_date.lte={expiry_before}"
    url = (f"{BASE}/v3/snapshot/options/{underlying}"
           f"?limit={limit}{flt}&apiKey={api_key}")
    out, spot, pages = [], 0.0, 0
    while url and pages < max_pages:
        r = httpx.get(url, timeout=45)
        r.raise_for_status()
        j = r.json()
        for c in j.get("results") or []:
            ua = c.get("underlying_asset") or {}
            if ua.get("price"):
                spot = float(ua["price"])
            out.append(c)
        nxt = j.get("next_url")
        url = f"{nxt}&apiKey={api_key}" if nxt else None
        pages += 1
    return out, spot


def build_profile(contracts: list[dict], spot: float, *,
                  model: str = "naive", per_point: bool = True,
                  expiries: int = 2,
                  strike_window: float = 0.06) -> dict:
    """Aggregate dealer gamma ($ per 1% move) by strike."""
    lo, hi = spot * (1 - strike_window), spot * (1 + strike_window)

    # keep the nearest N expiries — that's where gamma actually lives
    exps = sorted({(c.get("details") or {}).get("expiration_date")
                   for c in contracts
                   if (c.get("details") or {}).get("expiration_date")})
    keep_exp = set(exps[:expiries])

    by_strike: dict[float, float] = defaultdict(float)
    oi_by_strike: dict[float, float] = defaultdict(float)
    total_oi = 0

    for c in contracts:
        d = c.get("details") or {}
        g = c.get("greeks") or {}
        strike = d.get("strike_price")
        right = d.get("contract_type")
        exp = d.get("expiration_date")
        oi = c.get("open_interest") or 0
        gamma = g.get("gamma")
        if not strike or not right or gamma is None or not oi:
            continue
        if exp not in keep_exp or not (lo <= strike <= hi):
            continue

        # $ gamma per 1% move
        # per-point (public GEX convention): gamma * OI * 100 * spot
        # per-1%: multiply by spot * 0.01 as well
        gex = gamma * oi * 100 * spot
        if not per_point:
            gex *= spot * 0.01
        if model == "naive":
            signed = gex if right == "call" else -gex
        else:                                  # short_all
            signed = -gex
        by_strike[float(strike)] += signed
        oi_by_strike[float(strike)] += oi
        total_oi += oi

    strikes = sorted(by_strike)
    if not strikes:
        return {}

    net = sum(by_strike.values())

    # flip: cumulative zero-crossing walking up strikes
    cum, flip = 0.0, None
    prev_k, prev_c = None, None
    for k in strikes:
        cum += by_strike[k]
        if prev_c is not None and (prev_c < 0 <= cum or prev_c > 0 >= cum):
            span = cum - prev_c
            flip = prev_k + (k - prev_k) * (abs(prev_c) / abs(span)) if span else k
            break
        prev_k, prev_c = k, cum

    pos = {k: v for k, v in by_strike.items() if v > 0}
    neg = {k: v for k, v in by_strike.items() if v < 0}
    below = [k for k in strikes if k < spot]
    above = [k for k in strikes if k > spot]

    return {
        "spot": spot,
        "net": net,
        "flip": flip,
        "by_strike": by_strike,
        "total_oi": total_oi,
        "expiries": sorted(keep_exp),
        "max_magnet": max(pos, key=pos.get) if pos else None,
        "max_accel": min(neg, key=neg.get) if neg else None,
        "put_wall": (min((k for k in below), key=lambda k: by_strike[k])
                     if below else None),
        "call_wall": (max((k for k in above), key=lambda k: by_strike[k])
                      if above else None),
        "first_positive_above": next((k for k in above if by_strike[k] > 0), None),
    }


def level_label(gamma_at_strike: float, below_spot: bool) -> str:
    """Name a level by what dealer hedging actually does there.

    positive gamma -> dealers lean against moves -> support / resistance
    negative gamma -> dealers amplify moves      -> trapdoor / launchpad
    """
    if gamma_at_strike > 0:
        return "SUPPORT" if below_spot else "RESISTANCE"
    return "TRAPDOOR" if below_spot else "LAUNCHPAD"


def render(p: dict, model: str) -> None:
    if not p:
        console.print("[red]No chain data returned — check entitlement/underlying.")
        return
    spot, net = p["spot"], p["net"]
    flip = p["flip"]
    regime = "POSITIVE (pinning)" if net > 0 else "NEGATIVE (amplifying)"
    rel = ("spot ABOVE flip" if flip and spot > flip else
           "spot BELOW flip" if flip else "no flip in window")

    console.print()
    console.rule(f"[bold]SPX GEX — {dt.datetime.now():%Y-%m-%d %H:%M} "
                 f"· model={model} · exp {', '.join(p['expiries'])}")
    console.print(
        f"  spot [bold]{spot:,.0f}[/bold]   "
        f"net GEX [bold]{net/1e6:+,.0f}M[/bold] ({regime})   "
        f"flip [bold]{flip:,.0f}[/bold] ({rel})"
        if flip else
        f"  spot [bold]{spot:,.0f}[/bold]   net GEX [bold]{net/1e6:+,.0f}M[/bold] ({regime})")

    t = Table(show_header=True, header_style="bold")
    t.add_column("strike", justify="right")
    t.add_column("$GEX (M)", justify="right")
    t.add_column("bar")
    t.add_column("note")

    ordered = sorted(p["by_strike"].items(), key=lambda kv: -kv[0])
    peak = max(abs(v) for _, v in ordered) or 1.0
    bs = p["by_strike"]
    notes = {}
    if p.get("max_magnet"):
        notes[p["max_magnet"]] = "MAX MAGNET (pins)"
    if p.get("max_accel"):
        notes[p["max_accel"]] = "MAX ACCELERATOR (amplifies)"
    if p.get("put_wall"):
        k = p["put_wall"]
        notes.setdefault(k, f"largest below — {level_label(bs.get(k, 0), True)}")
    if p.get("call_wall"):
        k = p["call_wall"]
        notes.setdefault(k, f"largest above — {level_label(bs.get(k, 0), False)}")
    if p.get("first_positive_above"):
        notes.setdefault(p["first_positive_above"], "first positive gamma above")
    for k, v in ordered:
        if abs(v) < peak * 0.04:
            continue
        width = int(abs(v) / peak * 28)
        bar = ("[green]" if v > 0 else "[red]") + "█" * max(width, 1)
        note = notes.get(k, "")
        if abs(k - spot) <= 2.5:
            note = (note + "  ◄ SPOT").strip()
        t.add_row(f"{k:,.0f}", f"{v/1e6:+,.0f}", bar, note)
    console.print(t)
    console.print(f"  contracts aggregated: {p['total_oi']:,} OI\n")


def fetch_index_spot(ticker: str, api_key: str) -> float:
    """Index snapshots don't carry underlying price on option contracts —
    fetch the index value directly."""
    r = httpx.get(f"{BASE}/v3/snapshot/indices?ticker={ticker}&apiKey={api_key}",
                  timeout=30)
    if r.status_code == 200:
        for res in (r.json().get("results") or []):
            v = res.get("value") or (res.get("session") or {}).get("close")
            if v:
                return float(v)
    # fallback: last minute aggregate
    today = dt.date.today()
    for back in range(0, 5):
        d = today - dt.timedelta(days=back)
        rr = httpx.get(f"{BASE}/v2/aggs/ticker/{ticker}/range/1/minute/{d}/{d}"
                       f"?sort=desc&limit=1&apiKey={api_key}", timeout=30)
        if rr.status_code == 200 and (rr.json().get("results") or []):
            return float(rr.json()["results"][0]["c"])
    return 0.0


def show_levels(underlying: str = "I:SPX", model: str = "naive",
                expiries: int = 2, window: float = 0.06,
                per_point: bool = True) -> None:
    key = os.getenv("MASSIVE_API_KEY", "")
    if not key or key == "your_key_here":
        console.print("[red]MASSIVE_API_KEY not set")
        return
    console.print(f"[dim]fetching {underlying} chain…")
    contracts, spot = fetch_chain(underlying, key)
    if not spot:
        spot = fetch_index_spot(underlying, key)
    console.print(f"[dim]{len(contracts):,} contracts, spot {spot:,.2f}")
    if not spot:
        console.print("[red]Could not determine spot — aborting")
        return
    if not contracts:
        return
    render(build_profile(contracts, spot, model=model, per_point=per_point,
                         expiries=expiries, strike_window=window), model)
