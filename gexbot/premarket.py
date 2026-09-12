"""Overnight / pre-market summary (spec §10.4).

The operator checks by hand which levels were touched after the close. The
engine cannot help during the session — `watch` sleeps outside 09:00-16:15 and
open interest is static overnight anyway — so this runs once before the open
and reports what happened against the map that was last computed.

**Strictly descriptive.** It says which levels the overnight range reached and
where spot sits against them. It does not say what that means, and it must not
grow a sentence that does: GEX is a volatility-regime measure, and this
project's own testing found the entry logic did not beat random out of sample
(CLAUDE.md §2). A pre-market note that editorialised would be a directional
call arriving before the operator has formed one.
"""
from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass

import duckdb
import httpx
from rich.console import Console

from .clock import ET
from .instruments import INSTRUMENTS, SPY, observed_ratio
from .state import DEFAULT_UNDERLYING, default_path

console = Console()
BASE = "https://api.polygon.io"
SESSION_CLOSE_MIN = 16 * 60


@dataclass
class Overnight:
    ticker: str
    high: float
    low: float
    last: float
    prior_close: float
    bars: int


def fetch_overnight(api_key: str, day: dt.date, *, ticker: str = "SPY") -> Overnight | None:
    """SPY extended-hours minute bars from the prior close to now.

    SPY rather than ES: extended-hours SPY aggregates are already covered by
    the existing subscription, and §10.5 is explicit that no futures
    entitlement should be added for this.
    """
    frm = day - dt.timedelta(days=4)          # cover a long weekend
    url = (f"{BASE}/v2/aggs/ticker/{ticker}/range/1/minute/{frm}/{day}"
           f"?adjusted=true&sort=asc&limit=50000&apiKey={api_key}")
    try:
        r = httpx.get(url, timeout=45)
        if r.status_code != 200:
            return None
        rows = (r.json() or {}).get("results") or []
    except httpx.HTTPError:
        return None
    if not rows:
        return None

    def et(ms: int) -> dt.datetime:
        return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc).astimezone(ET)

    # the last regular-session close before `day`, then everything after it
    regular = [b for b in rows
               if et(b["t"]).date() < day
               and et(b["t"]).hour * 60 + et(b["t"]).minute <= SESSION_CLOSE_MIN]
    if not regular:
        return None
    prior_close = float(regular[-1]["c"])
    cutoff = regular[-1]["t"]
    after = [b for b in rows if b["t"] > cutoff]
    if not after:
        return None
    return Overnight(ticker=ticker,
                     high=max(float(b["h"]) for b in after),
                     low=min(float(b["l"]) for b in after),
                     last=float(after[-1]["c"]),
                     prior_close=prior_close,
                     bars=len(after))


def last_profile(db_path, session_date: dt.date | None = None,
                 underlying: str = DEFAULT_UNDERLYING) -> dict | None:
    """The final stored map of the most recent session at or before `date`."""
    with duckdb.connect(str(db_path), read_only=True) as con:
        sql = ("SELECT * FROM poll_snapshot WHERE underlying = ?"
               + ("" if session_date is None else " AND session_date <= ?")
               + " ORDER BY poll_id DESC LIMIT 1")
        params = [underlying] + ([] if session_date is None else [session_date])
        cur = con.execute(sql, params)
        cols = [d[0] for d in cur.description]
        row = cur.fetchone()
        if row is None:
            return None
        poll = dict(zip(cols, row))
        cur = con.execute(
            "SELECT strike, gex FROM poll_strike WHERE poll_id=? ORDER BY strike",
            [poll["poll_id"]])
        poll["strikes"] = [{"strike": r[0], "gex": r[1]} for r in cur.fetchall()]
    return poll


def summarise(poll: dict, on: Overnight, ratio: float | None = None) -> dict:
    """Which levels the overnight range reached, and where spot sits now.

    The map is in SPX points and the range is in SPY, so the range is scaled
    up rather than the map scaled down — the map is what was measured, and
    converting it would quietly restate the thing being compared against.
    """
    # Measured, not assumed (§10.3 amendment). Falls back to the constant only
    # when no SPY poll exists to measure against, and says which was used.
    m = ratio if ratio else INSTRUMENTS[SPY].strike_to_spx
    hi, lo, last, prior = on.high * m, on.low * m, on.last * m, on.prior_close * m

    from .levels import level_label
    by_strike = {s["strike"]: s["gex"] for s in poll["strikes"]}
    named = [(k, poll.get(k)) for k in
             ("flip", "put_wall", "call_wall", "max_accel", "max_magnet")]

    touched = []
    for kind, strike in named:
        if strike is None:
            continue
        gex = by_strike.get(strike, 0.0)
        within = lo <= strike <= hi
        crossed = (prior < strike <= last) or (last <= strike < prior)
        if within or crossed:
            touched.append({
                "kind": kind, "strike": strike,
                "label": level_label(gex, strike < poll["spot"]),
                "gex": gex,
                "status": "breached" if crossed else "touched",
            })

    nearest = sorted(
        ({"strike": k, "gex": v, "label": level_label(v, k < last),
          "distance": k - last} for k, v in by_strike.items() if v),
        key=lambda x: abs(x["distance"]))[:3]

    # SPY x10 is NOT SPX. The ETF tracks the index with a persistent basis
    # (dividends, expense, tracking), measured here rather than assumed away:
    # at ~0.2% it is ~15 SPX points, which is wider than the proximity window
    # a level alert uses. Reported so the reader can discount accordingly.
    basis_pts = poll["spot"] - prior
    return {"session_date": poll["session_date"], "map_spot": poll["spot"],
            "ratio": m, "ratio_measured": bool(ratio),
            "basis_pts": basis_pts,
            "basis_pct": basis_pts / poll["spot"] if poll["spot"] else 0.0,
            "overnight_high": hi, "overnight_low": lo, "last": last,
            "prior_close": prior, "range_pts": hi - lo,
            "range_pct": (hi - lo) / prior if prior else 0.0,
            "change_pts": last - prior,
            "change_pct": (last - prior) / prior if prior else 0.0,
            "touched": touched, "nearest": nearest, "bars": on.bars}


def render(s: dict) -> str:
    def pts(v: float) -> str:
        return f"{v:+,.0f}"
    lines = [
        f"**Overnight** · {s['bars']:,} SPY extended-hours minutes, "
        f"shown at SPX scale",
        f"Range **{s['overnight_low']:,.0f} – {s['overnight_high']:,.0f}** "
        f"({s['range_pts']:,.0f} pts, {s['range_pct']:.2%})",
        f"Now **{s['last']:,.0f}** · {pts(s['change_pts'])} pts "
        f"({s['change_pct']:+.2%}) from the {s['session_date']} close",
        "",
        f"**Against the {s['session_date']} map** (last stored profile, "
        f"spot then {s['map_spot']:,.0f})",
    ]
    if s["touched"]:
        for t in s["touched"]:
            lines.append(f"· **{t['strike']:,.0f}** {t['kind'].replace('_',' ')} "
                         f"— {t['label']} — {t['status']}")
    else:
        lines.append("· no computed level was reached overnight")
    lines.append("")
    lines.append("**Nearest structure to current price**")
    for n in s["nearest"]:
        lines.append(f"· **{n['strike']:,.0f}** {n['label']} "
                     f"({n['distance']:+,.0f} pts)")
    lines += ["",
              f"_Scaling: SPY x{s['ratio']:.4f} "
              f"({'measured from the same session' if s['ratio_measured'] else 'ASSUMED — no SPY poll stored'}). "
              f"Residual basis to the map {s['basis_pts']:+,.0f} SPX pts "
              f"({s['basis_pct']:+.2%})._",
              "_Descriptive only. Open interest is static overnight, so this "
              "map is yesterday's positioning, not today's._"]
    return "\n".join(lines)


def run_premarket(*, day: dt.date | None = None, dry_run: bool = False,
                  db_path=None) -> int:
    from dotenv import load_dotenv
    load_dotenv()
    key = os.getenv("MASSIVE_API_KEY", "")
    if not key or key == "your_key_here":
        console.print("[red]MASSIVE_API_KEY not set")
        return 1

    day = day or dt.datetime.now(dt.timezone.utc).astimezone(ET).date()
    db = db_path or default_path()

    poll = last_profile(db) if str(db) and __import__("pathlib").Path(db).exists() else None
    if poll is None:
        console.print("[yellow]No stored map to compare against — has the "
                      "engine ever run a session?")
        return 1

    on = fetch_overnight(key, day)
    if on is None:
        console.print("[yellow]No extended-hours bars returned for SPY")
        return 1

    spy_poll = last_profile(db, poll["session_date"], underlying=SPY)
    ratio = observed_ratio(poll["spot"], (spy_poll or {}).get("spot"), 0.0)
    body = render(summarise(poll, on, ratio or None))
    console.print(body)

    if dry_run:
        console.print("\n[dim]--dry-run: nothing posted")
        return 0

    from .notify import from_settings
    from .state import StateStore
    n = from_settings()
    if not n.webhooks:
        console.print("[yellow]No Discord webhooks configured — printed only")
        return 0
    n.daily_digest(body=body, green_day=True)
    n.flush()
    StateStore(db).write_alert(channel="daily", kind="digest",
                               title="Pre-market summary", body=body)
    console.print("[green]posted to #daily")
    return 0
