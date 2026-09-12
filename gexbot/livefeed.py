"""Live flow ingester — the piece that makes the map *today's*.

Connects to Massive's options WebSocket, subscribes to SPX/SPXW trades,
classifies each print with the tick rule, converts it to dealer gamma, and
maintains a live flow overlay on top of the morning's OI baseline.

Why this matters: open interest publishes once pre-market and reflects
yesterday's close. Roughly half of SPX volume is 0DTE/1DTE, which never
appears in OI before it expires. An OI-only map is therefore structurally
blind to the day's dominant positioning. This module is the fix.

Architecture:
    WebSocket  ──► TradeClassifier ──► FlowLedger (per-strike dealer gamma)
                                            │
    OI snapshot (pre-market, once) ─────────┴──► CombinedProfile
                                                      │
                                            watch.py / dashboard read this

Isolation rules (spec §15.2): the feed thread never raises into the caller,
reconnects with backoff, and reports staleness so consumers can halt on
their own terms rather than trusting silent data.
"""
from __future__ import annotations

import json
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

from rich.console import Console

console = Console()

WS_URL = "wss://socket.polygon.io/options"
SESSION_ROOTS = ("SPX", "SPXW")


# ── trade classification ─────────────────────────────────────────────
class TickRuleClassifier:
    """Sign each print against the prior print in the SAME contract.
    +1 uptick (customer buy), -1 downtick (customer sell), zero-ticks
    inherit the last nonzero direction. Needs no quote feed."""

    def __init__(self) -> None:
        self._last_px: dict[str, float] = {}
        self._last_side: dict[str, int] = {}

    def classify(self, ticker: str, price: float) -> int:
        prev = self._last_px.get(ticker)
        self._last_px[ticker] = price
        if prev is None:
            return 0
        if price > prev:
            side = 1
        elif price < prev:
            side = -1
        else:
            side = self._last_side.get(ticker, 0)
        if side:
            self._last_side[ticker] = side
        return side


# ── live flow ledger ─────────────────────────────────────────────────
@dataclass
class FlowLedger:
    """Per-strike dealer gamma added by TODAY's classified flow, in the same
    per-point units as levels.build_profile so the two can be summed.

    Convention: a customer BUY makes dealers shorter gamma at that strike;
    a customer SELL makes them longer. Sign is independent of call/put —
    gamma is gamma.
    """
    gamma_by_strike: dict[float, float] = field(default_factory=lambda: defaultdict(float))
    contracts_seen: int = 0
    trades_seen: int = 0
    last_trade_ts: float = 0.0
    # cumulative session-to-date premium, four buckets (spec §3.3)
    call_bought: float = 0.0
    call_sold: float = 0.0
    put_bought: float = 0.0
    put_sold: float = 0.0
    premium_trades: int = 0
    unclassified: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, strike: float, size: int, side: int, gamma: float,
            spot: float) -> float:
        """Returns the signed dealer-gamma delta actually applied.

        Returned rather than recomputed by the caller so the tape's
        `dealer_gamma_delta` column is the number the ledger really wrote. A
        second copy of this arithmetic would be a second place for the map and
        its own diagnostic to disagree — which is precisely what the column
        exists to detect.
        """
        if not side or gamma <= 0:
            return 0.0
        # per-point dollar gamma for this print
        dollars = gamma * size * 100 * spot
        delta = -side * dollars
        with self._lock:
            self.gamma_by_strike[strike] += delta
            self.contracts_seen += size
            self.trades_seen += 1
            self.last_trade_ts = time.time()
        return delta

    def add_premium(self, right: str, size: int, price: float, side: int) -> None:
        """Accumulate premium into four buckets, keeping bought and sold apart.

        Unsigned totals conflate "paid $9.6M for puts" with "sold $9.6M of
        puts", which are opposite positions. Zero-tick prints that inherit no
        direction land in `unclassified` rather than being silently dropped or
        guessed into a side — the count is surfaced so a session with poor
        classification is visibly less meaningful.
        """
        dollars = price * size * 100.0
        with self._lock:
            if not side:
                self.unclassified += 1
                return
            self.premium_trades += 1
            if right == "C":
                if side > 0:
                    self.call_bought += dollars
                else:
                    self.call_sold += dollars
            elif side > 0:
                self.put_bought += dollars
            else:
                self.put_sold += dollars

    def premium_snapshot(self) -> dict:
        with self._lock:
            return {"call_bought": self.call_bought, "call_sold": self.call_sold,
                    "put_bought": self.put_bought, "put_sold": self.put_sold,
                    "trades_counted": self.premium_trades,
                    "unclassified": self.unclassified}

    def snapshot(self) -> dict[float, float]:
        with self._lock:
            return dict(self.gamma_by_strike)

    def net(self) -> float:
        with self._lock:
            return sum(self.gamma_by_strike.values())

    def staleness(self) -> float:
        return time.time() - self.last_trade_ts if self.last_trade_ts else float("inf")

    def reset(self) -> None:
        """Called on a session-date rollover: these are session-to-date
        totals, and carrying yesterday's into today would be a lie."""
        with self._lock:
            self.gamma_by_strike.clear()
            self.contracts_seen = self.trades_seen = 0
            self.call_bought = self.call_sold = 0.0
            self.put_bought = self.put_sold = 0.0
            self.premium_trades = self.unclassified = 0


# ── the tape (spec §15) ──────────────────────────────────────────────
@dataclass
class TapePrint:
    """One classified print, unaggregated.

    `gamma_used` and `dealer_gamma_delta` are diagnostics, not decoration. A
    gamma lookup that returns 0 for every print produces a flat overlay that
    looks exactly like a quiet market — the key-shape mismatch found while
    wiring the flow overlay would have been obvious here and invisible
    anywhere else.
    """
    ts: float
    ticker: str
    root: str
    expiry: str
    strike: float
    right: str
    price: float
    size: int
    side: int                    # +1 buy, -1 sell, 0 unclassified
    premium: float
    gamma_used: float
    dealer_gamma_delta: float


@dataclass
class TapeBuffer:
    """Bounded ring of recent prints, plus the session's honesty counters.

    `deque(maxlen=...)` is the bound: it cannot grow past the cap no matter
    how long the session runs or how fast SPX prints arrive.
    """
    cap: int = 2000
    prints_seen: int = 0
    contracts_seen: int = 0
    unclassified: int = 0
    gamma_misses: int = 0
    _buf: deque = field(default_factory=lambda: deque(maxlen=2000), repr=False)
    _pending: deque = field(default_factory=lambda: deque(maxlen=2000), repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        if self.cap != 2000:
            self._buf = deque(maxlen=self.cap)
            self._pending = deque(maxlen=self.cap)

    def add(self, p: TapePrint) -> None:
        with self._lock:
            self._buf.append(p)
            self._pending.append(p)
            self.prints_seen += 1
            self.contracts_seen += p.size
            if not p.side:
                self.unclassified += 1
            elif p.gamma_used == 0.0:
                # classified, but the lookup had no gamma for the contract, so
                # the print moved nothing. Counted separately from unclassified
                # because the causes and the fixes are different.
                self.gamma_misses += 1

    def drain(self) -> list[TapePrint]:
        """Prints since the last drain. Used to batch onto the 1 s push."""
        with self._lock:
            out = list(self._pending)
            self._pending.clear()
            return out

    def recent(self, limit: int = 200) -> list[TapePrint]:
        with self._lock:
            return list(self._buf)[-limit:][::-1]        # newest first

    def stats(self) -> dict:
        with self._lock:
            seen = self.prints_seen
            return {"prints_seen": seen, "contracts_seen": self.contracts_seen,
                    "unclassified": self.unclassified,
                    "unclassified_pct": (self.unclassified / seen) if seen else None,
                    "gamma_misses": self.gamma_misses,
                    "gamma_miss_pct": (self.gamma_misses / seen) if seen else None,
                    "buffered": len(self._buf), "cap": self.cap}

    def reset(self) -> None:
        with self._lock:
            self._buf.clear()
            self._pending.clear()
            self.prints_seen = self.contracts_seen = 0
            self.unclassified = self.gamma_misses = 0


# ── websocket feed ───────────────────────────────────────────────────
class OptionsFeed:
    """Background WebSocket client. Never raises into the caller; reconnects
    with exponential backoff; tracks connection health."""

    def __init__(self, api_key: str, ledger_for, gamma_fn, spot_fn, *,
                 roots=SESSION_ROOTS, on_status=None, tape_for=None):
        """`ledger_for(root)` picks the book a print belongs to.

        One book per instrument, never one shared book: a SPY print at strike
        765 and an SPX print at 7,650 describe the same price level but are
        different contracts, and adding them into one per-strike dict would
        silently corrupt both maps. `gamma_fn` takes the root for the same
        reason — the gamma of "the 765 call" is meaningless without it.

        A plain FlowLedger is accepted for the single-instrument case.
        """
        self.api_key = api_key
        if isinstance(ledger_for, FlowLedger):
            single = ledger_for
            self.ledger_for = lambda _root: single
            self.ledger = single
        else:
            self.ledger_for = ledger_for
            self.ledger = None
        self.gamma_fn = gamma_fn          # (root, strike, right, expiry) -> gamma
        self.spot_fn = spot_fn            # (root) -> current spot
        self.tape_for = tape_for          # (root) -> TapeBuffer | None
        self.roots = roots
        self.on_status = on_status
        self.classifier = TickRuleClassifier()
        self.connected = False
        self.messages = 0
        self.reconnects = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ── internals ────────────────────────────────────────────────────
    def _run(self) -> None:
        backoff = 1
        while not self._stop.is_set():
            try:
                self._session()
                backoff = 1
            except Exception as e:                     # never propagate
                self.connected = False
                self.reconnects += 1
                if self.on_status:
                    self.on_status("disconnected", str(e))
                console.log(f"[yellow]feed disconnected: {e} "
                            f"(retry in {backoff}s)")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _session(self) -> None:
        from websockets.sync.client import connect        # lazy import

        with connect(WS_URL, open_timeout=20, close_timeout=5) as ws:
            ws.send(json.dumps({"action": "auth", "params": self.api_key}))
            # subscribe to trades for our roots (T.O:SPXW* wildcard)
            subs = ",".join(f"T.O:{r}*" for r in self.roots)
            ws.send(json.dumps({"action": "subscribe", "params": subs}))
            self.connected = True
            if self.on_status:
                self.on_status("connected", subs)
            console.log(f"[green]feed connected · subscribed {subs}")

            while not self._stop.is_set():
                raw = ws.recv(timeout=30)
                for msg in json.loads(raw):
                    self._handle(msg)

    def _handle(self, msg: dict) -> None:
        if msg.get("ev") != "T":
            return
        self.messages += 1
        ticker = msg.get("sym") or ""
        price = msg.get("p")
        size = int(msg.get("s") or 0)
        if not ticker or price is None or size <= 0:
            return
        side = self.classifier.classify(ticker, float(price))
        parsed = _parse(ticker)
        if not parsed:
            return
        root, expiry, right, strike = parsed
        if root not in self.roots:
            return
        ledger = self.ledger_for(root)
        if ledger is None:
            return
        # Premium counts every print in our roots, side or no side: the
        # unclassified tally is only honest if the zero-ticks reach it.
        ledger.add_premium(right, size, float(price), side)

        gamma = self.gamma_fn(root, strike, right, expiry) if side else 0.0
        delta = (ledger.add(strike, size, side, gamma, self.spot_fn(root))
                 if (side and gamma) else 0.0)

        # The tape sees every print, including the ones that changed nothing —
        # that is the whole diagnostic value.
        tape = self.tape_for(root) if self.tape_for else None
        if tape is not None:
            tape.add(TapePrint(
                ts=time.time(), ticker=ticker, root=root, expiry=expiry,
                strike=strike, right=right, price=float(price), size=size,
                side=side, premium=float(price) * size * 100.0,
                gamma_used=float(gamma or 0.0), dealer_gamma_delta=delta))


def _parse(ticker: str):
    """O:SPXW260911C07600000 -> (root, 'YYMMDD', 'C', 7600.0)"""
    t = ticker[2:] if ticker.startswith("O:") else ticker
    if len(t) < 16:
        return None
    root, tail = t[:-15], t[-15:]
    date_s, right, strike_s = tail[:6], tail[6], tail[7:]
    if right not in ("C", "P") or not strike_s.isdigit():
        return None
    return root, date_s, right, int(strike_s) / 1000.0


# ── combining OI baseline with live flow ─────────────────────────────
def gamma_lookup(contracts: list[dict], root: str | None = None) -> dict[tuple, float]:
    """Per-contract gamma from a chain snapshot, keyed the way the feed asks.

    `OptionsFeed` parses OPRA tickers into (root, 'YYMMDD', 'C'|'P', strike),
    so the key has to be in those terms rather than the snapshot's own
    ('call', '2026-09-14'). A strike outside the fetched window has no gamma
    here and its flow is skipped — which is correct, not a gap: the OI
    baseline is bounded by the same window, and inventing a gamma for a
    strike we never priced would put fiction into the overlay.
    """
    out: dict[tuple, float] = {}
    for c in contracts:
        d = c.get("details") or {}
        g = (c.get("greeks") or {}).get("gamma")
        strike, right, exp = (d.get("strike_price"), d.get("contract_type"),
                              d.get("expiration_date"))
        if g is None or strike is None or right not in ("call", "put") or not exp:
            continue
        yymmdd = f"{exp[2:4]}{exp[5:7]}{exp[8:10]}"      # 2026-09-14 -> 260914
        key = (float(strike), "C" if right == "call" else "P", yymmdd)
        out[key if root is None else (root, *key)] = float(g)
    return out


def combine(oi_profile: dict, flow: dict[float, float]) -> dict:
    """Sum the OI baseline and today's flow overlay, recompute levels.

    Returns the same shape as levels.build_profile, plus flow diagnostics and
    — importantly for the state store — the per-strike SPLIT that produced
    the merged number.

    The split covers every merged strike with an explicit 0.0 rather than
    leaving gaps. Once the overlay is running, both components are measured
    at every strike in the window: a strike with no classified trades has a
    flow of exactly zero, not an unknown one. That keeps NULL in
    `poll_strike` meaning one thing only — the overlay was off — instead of
    being ambiguous between "not measured" and "measured as nothing".
    """
    oi_baseline = dict(oi_profile.get("by_strike") or {})
    merged = dict(oi_baseline)
    for k, v in flow.items():
        merged[k] = merged.get(k, 0.0) + v

    spot = oi_profile["spot"]
    strikes = sorted(merged)
    if not strikes:
        return oi_profile

    net = sum(merged.values())
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

    out = dict(oi_profile)
    out.update({
        "by_strike": merged,
        "net": net,
        "flip": flip,
        "max_magnet": max(pos, key=pos.get) if pos else None,
        "max_accel": min(neg, key=neg.get) if neg else None,
        "put_wall": min(below, key=lambda k: merged[k]) if below else None,
        "call_wall": max(above, key=lambda k: merged[k]) if above else None,
        "first_positive_above": next((k for k in above if merged[k] > 0), None),
        "flow_net": sum(flow.values()),
        "flow_strikes": len(flow),
        "oi_net": oi_profile.get("net", 0.0),
        "oi_by_strike": {k: float(oi_baseline.get(k, 0.0)) for k in merged},
        "volume_by_strike": dict(oi_profile.get("volume_by_strike") or {}),
        "flow_by_strike": {k: float(flow.get(k, 0.0)) for k in merged},
    })
    return out
