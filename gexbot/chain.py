"""Chain view for the entry surface (exit-manager spec §4).

Deliberately minimal: this is not a trading terminal. Strike, bid/ask, mid,
spread width, and the GEX at that strike — enough to choose a contract and
nothing more. No chart; the operator uses TradingView.

XSP earns its place here rather than being an afterthought. At a tenth the
notional a three-contract position is affordable at this capital, which is
what makes the §2.5 ladder possible at all. Its thinner book is a real cost,
so the spread is shown next to every strike rather than buried — the same
number §2.6 makes the shadow record carry.

XSP strikes are shown with their SPX equivalent so the two chains can be read
in one frame. That conversion is display only; the quotes are XSP's own.
"""
from __future__ import annotations

import datetime as dt

from .levels import build_profile, fetch_chain, fetch_index_spot

# Both are cash-settled European options on the same index; XSP is 1/10th.
SYMBOLS = {
    "SPX": {"chain": "I:SPX", "spx_multiple": 1.0, "tick": 0.05},
    "XSP": {"chain": "XSP", "spx_multiple": 10.0, "tick": 0.01},
}


def _quote(c: dict) -> dict:
    q = c.get("last_quote") or {}
    bid, ask = q.get("bid"), q.get("ask")
    if bid is None or ask is None or ask < bid:
        day = c.get("day") or {}
        close = day.get("close")
        return {"bid": None, "ask": None, "mid": close, "spread": None}
    return {"bid": bid, "ask": ask, "mid": (bid + ask) / 2.0,
            "spread": round(ask - bid, 4)}


def pick_expiry(expiries: list[str], dte: int) -> str | None:
    """Nearest listed expiry at or beyond `dte` calendar days."""
    today = dt.date.today()
    dated = []
    for e in expiries:
        try:
            dated.append((dt.date.fromisoformat(e) - today).days)
        except ValueError:
            continue
    if not dated:
        return None
    ok = sorted((d, e) for d, e in zip(dated, sorted(expiries)) if d >= dte)
    return ok[0][1] if ok else sorted(expiries)[-1]


def chain_view(symbol: str, api_key: str, *, dte: int = 3, strikes: int = 5,
               window: float = 0.06) -> dict:
    """ATM ± `strikes` for one symbol, with the GEX at each strike.

    Gamma comes from the engine's own profile, never recomputed here — the
    chain view and the map must agree about what is at 7,650.

    XSP carries a measured caveat. Its option snapshots publish **no greeks at
    all** (0 of 2,918 contracts, checked live), so a gamma profile cannot be
    built from XSP's own chain. Rather than show an empty column, each XSP
    strike shows the SPX map's gamma at the equivalent level, tagged
    `gex_source: "spx_map"` so the UI can say so. That substitution is sound
    here in a way the SPY one is not: XSP is one tenth of the *same index* by
    contract definition, so ×10 is exact and carries none of the tracking
    basis that makes SPY×10 wrong (CLAUDE.md §9).
    """
    if symbol not in SYMBOLS:
        raise ValueError(f"unknown symbol {symbol!r}; expected SPX or XSP")
    meta = SYMBOLS[symbol]

    spot_hint = fetch_index_spot("I:SPX", api_key) if symbol == "SPX" else 0.0
    if symbol == "XSP" and spot_hint == 0.0:
        spx = fetch_index_spot("I:SPX", api_key)
        spot_hint = spx / 10.0 if spx else 0.0

    exp_cap = (dt.date.today() + dt.timedelta(days=dte + 21)).isoformat()
    contracts, chain_spot = fetch_chain(meta["chain"], api_key,
                                        spot_hint=spot_hint,
                                        strike_window=window,
                                        expiry_before=exp_cap)
    spot = chain_spot or spot_hint
    if not spot or not contracts:
        return {"symbol": symbol, "spot": None, "rows": [], "levels": {},
                "expiry": None, "detail": "no chain data returned"}

    expiry = pick_expiry(
        sorted({(c.get("details") or {}).get("expiration_date")
                for c in contracts
                if (c.get("details") or {}).get("expiration_date")}), dte)

    profile = build_profile(contracts, spot, model="naive", per_point=True,
                            expiries=2, strike_window=window) or {}
    gex_by_strike = profile.get("by_strike") or {}
    gex_source = "own"

    if not gex_by_strike and symbol != "SPX":
        # No greeks published for this symbol — fall back to the SPX map at the
        # equivalent level, and say so rather than showing a blank column.
        spx_spot = fetch_index_spot("I:SPX", api_key)
        spx_contracts, csp = fetch_chain("I:SPX", api_key, spot_hint=spx_spot,
                                         strike_window=window,
                                         expiry_before=exp_cap)
        spx_profile = build_profile(spx_contracts, csp or spx_spot,
                                    model="naive", per_point=True, expiries=2,
                                    strike_window=window) or {}
        spx_by_strike = spx_profile.get("by_strike") or {}
        m = meta["spx_multiple"]
        gex_by_strike = {k / m: v for k, v in spx_by_strike.items()}
        profile = {**spx_profile,
                   **{k: (spx_profile.get(k) / m
                          if spx_profile.get(k) is not None else None)
                      for k in ("flip", "put_wall", "call_wall", "max_accel",
                                "max_magnet", "first_positive_above")}}
        gex_source = "spx_map" if gex_by_strike else "unavailable"

    # ATM +/- N listed strikes on the chosen expiry
    on_expiry = [c for c in contracts
                 if (c.get("details") or {}).get("expiration_date") == expiry]
    all_strikes = sorted({(c.get("details") or {}).get("strike_price")
                          for c in on_expiry
                          if (c.get("details") or {}).get("strike_price")})
    if not all_strikes:
        return {"symbol": symbol, "spot": spot, "rows": [], "levels": {},
                "expiry": expiry, "detail": "no strikes on the chosen expiry"}
    atm = min(range(len(all_strikes)), key=lambda i: abs(all_strikes[i] - spot))
    keep = all_strikes[max(0, atm - strikes): atm + strikes + 1]

    by_key: dict[tuple, dict] = {}
    for c in on_expiry:
        d = c.get("details") or {}
        by_key[(d.get("strike_price"), d.get("contract_type"))] = c

    rows = []
    for k in keep:
        call, put = by_key.get((k, "call")), by_key.get((k, "put"))
        rows.append({
            "strike": k,
            "spx_equivalent": k * meta["spx_multiple"],
            "gex": gex_by_strike.get(k),
            "gex_source": gex_source,
            "call": _quote(call) if call else None,
            "put": _quote(put) if put else None,
            "atm": k == all_strikes[atm],
        })

    return {
        "symbol": symbol,
        "spot": spot,
        "spx_equivalent_spot": spot * meta["spx_multiple"],
        "expiry": expiry,
        "dte": (dt.date.fromisoformat(expiry) - dt.date.today()).days
               if expiry else None,
        "rows": rows,
        "gex_source": gex_source,
        "levels": {k: profile.get(k) for k in
                   ("flip", "put_wall", "call_wall", "max_accel", "max_magnet",
                    "first_positive_above")},
        "net_gex": profile.get("net"),
    }
