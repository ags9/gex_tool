"""Instrument definitions and the S&P-complex merge (spec §10.2, §10.3).

Three things the operator can look at, and they are NOT the same object:

  I:SPX    the SPX chain. What the engine has always computed.
  SPY      SPY's own chain, own OI, own gamma. SPY walls sit at genuinely
           different strikes than SPX walls — a second map, not a rescaling.
  COMPLEX  SPX + SPY merged into one book, SPY scaled to SPX notional.

The distinction §10.1 turns on: dividing the SPX map by ten is a *display
transform* and stays a view of SPX. Fetching SPY's chain is a different
measurement. The UI must never let the first masquerade as the second.

COMPLEX is a model change, not a completeness fix: it moves walls, shifts the
flip, and can change a regime call. Whether it predicts better is an open
empirical question, so it is never a default and never alerts.
"""
from __future__ import annotations

from dataclasses import dataclass

SPX = "I:SPX"
SPY = "SPY"
COMPLEX = "COMPLEX"


@dataclass(frozen=True)
class Instrument:
    key: str
    chain: str                 # what levels.fetch_chain is asked for
    roots: tuple[str, ...]     # OPRA roots for the live feed
    strike_to_spx: float       # multiply a strike by this to reach SPX scale
    label: str


INSTRUMENTS: dict[str, Instrument] = {
    SPX: Instrument(SPX, "I:SPX", ("SPX", "SPXW"), 1.0, "SPX"),
    SPY: Instrument(SPY, "SPY", ("SPY",), 10.0, "SPY"),
}


def instrument(key: str) -> Instrument:
    try:
        return INSTRUMENTS[key]
    except KeyError:
        raise SystemExit(f"unknown underlying {key!r}; "
                         f"expected one of {', '.join(INSTRUMENTS)} or {COMPLEX}")


def root_owner(root: str) -> str | None:
    """Which instrument a print belongs to. SPX and SPXW share one book."""
    for inst in INSTRUMENTS.values():
        if root in inst.roots:
            return inst.key
    return None


def to_spx_scale(profile: dict, inst: Instrument) -> dict:
    """Re-express a profile on the SPX strike axis.

    Two conversions, and they go in opposite directions:

      strike  x10   a SPY strike of 765 is the SPX 7,650 level
      gamma   /10   per-point dollar gamma is dollars per ONE point of the
                    underlying, and one SPY point is ten SPX points

    Getting the second one backwards would inflate SPY's contribution a
    hundredfold and quietly dominate the merged map, which is exactly the
    kind of error a "more complete" model is supposed to be suspected of.
    """
    m, spot = inst.strike_to_spx, profile.get("spot")
    if m == 1.0:
        return dict(profile)
    def conv(d: dict | None) -> dict:
        return {k * m: v / m for k, v in (d or {}).items()}
    out = dict(profile)
    out.update({
        "spot": spot * m if spot is not None else None,
        "by_strike": conv(profile.get("by_strike")),
        "oi_by_strike": conv(profile.get("oi_by_strike")),
        "flow_by_strike": conv(profile.get("flow_by_strike")),
        # volume is a contract count, not a dollar figure: the strike moves,
        # the count does not get divided
        "volume_by_strike": {k * m: v
                             for k, v in (profile.get("volume_by_strike") or {}).items()},
        "net": (profile.get("net") or 0.0) / m,
        "oi_net": None if profile.get("oi_net") is None else profile["oi_net"] / m,
        "flow_net": None if profile.get("flow_net") is None else profile["flow_net"] / m,
        **{k: (profile[k] * m if profile.get(k) is not None else None)
           for k in ("flip", "put_wall", "call_wall", "max_accel", "max_magnet",
                     "first_positive_above")},
    })
    return out


def merge_complex(profiles: dict[str, dict]) -> dict | None:
    """Sum instrument profiles on the SPX axis and recompute the levels.

    Levels are re-derived from the merged book rather than inherited: a wall
    is wherever the combined gamma actually peaks, which is the whole claim
    of the combined view. Inheriting SPX's walls would produce a chart that
    looks combined and is not.
    """
    scaled = [to_spx_scale(p, INSTRUMENTS[k])
              for k, p in profiles.items() if p and k in INSTRUMENTS]
    if not scaled:
        return None

    merged: dict[float, float] = {}
    oi_bs: dict[float, float] = {}
    flow_bs: dict[float, float] = {}
    vol_bs: dict[float, float] = {}
    for p in scaled:
        for d, acc in ((p.get("by_strike"), merged), (p.get("oi_by_strike"), oi_bs),
                       (p.get("flow_by_strike"), flow_bs),
                       (p.get("volume_by_strike"), vol_bs)):
            for k, v in (d or {}).items():
                acc[k] = acc.get(k, 0.0) + v

    # spot is the SPX-scale spot; the instruments agree to within basis points
    spot = sum(p["spot"] for p in scaled if p.get("spot")) / max(
        1, sum(1 for p in scaled if p.get("spot")))
    strikes = sorted(merged)
    if not strikes:
        return None

    cum, flip, prev_k, prev_c = 0.0, None, None, None
    for k in strikes:
        cum += merged[k]
        if prev_c is not None and (prev_c < 0 <= cum or prev_c > 0 >= cum):
            span = cum - prev_c
            flip = prev_k + (k - prev_k) * (abs(prev_c) / abs(span)) if span else k
            break
        prev_k, prev_c = k, cum

    pos = {k: v for k, v in merged.items() if v > 0}
    neg = {k: v for k, v in merged.items() if v < 0}
    below = [k for k in strikes if k < spot]
    above = [k for k in strikes if k > spot]
    has_flow = any(p.get("flow_net") is not None for p in scaled)

    return {
        "spot": spot,
        "net": sum(merged.values()),
        "by_strike": merged,
        "oi_by_strike": oi_bs,
        "flow_by_strike": flow_bs if has_flow else {},
        "volume_by_strike": vol_bs,
        "oi_net": sum(oi_bs.values()) if oi_bs else None,
        "flow_net": sum(flow_bs.values()) if has_flow else None,
        "flip": flip,
        "put_wall": min(below, key=lambda k: merged[k]) if below else None,
        "call_wall": max(above, key=lambda k: merged[k]) if above else None,
        "max_magnet": max(pos, key=pos.get) if pos else None,
        "max_accel": min(neg, key=neg.get) if neg else None,
        "first_positive_above": next((k for k in above if merged[k] > 0), None),
        "expiries": sorted({e for p in scaled for e in (p.get("expiries") or [])}),
        "components": sorted(profiles),
    }
