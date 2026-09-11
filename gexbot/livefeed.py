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
from collections import defaultdict
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
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, strike: float, size: int, side: int, gamma: float,
            spot: float) -> None:
        if not side or gamma <= 0:
            return
        # per-point dollar gamma for this print
        dollars = gamma * size * 100 * spot
        with self._lock:
            self.gamma_by_strike[strike] += -side * dollars
            self.contracts_seen += size
            self.trades_seen += 1
            self.last_trade_ts = time.time()

    def snapshot(self) -> dict[float, float]:
        with self._lock:
            return dict(self.gamma_by_strike)

    def net(self) -> float:
        with self._lock:
            return sum(self.gamma_by_strike.values())

    def staleness(self) -> float:
        return time.time() - self.last_trade_ts if self.last_trade_ts else float("inf")

    def reset(self) -> None:
        with self._lock:
            self.gamma_by_strike.clear()
            self.contracts_seen = self.trades_seen = 0


# ── websocket feed ───────────────────────────────────────────────────
class OptionsFeed:
    """Background WebSocket client. Never raises into the caller; reconnects
    with exponential backoff; tracks connection health."""

    def __init__(self, api_key: str, ledger: FlowLedger,
                 gamma_fn, spot_fn, *, roots=SESSION_ROOTS,
                 on_status=None):
        self.api_key = api_key
        self.ledger = ledger
        self.gamma_fn = gamma_fn          # (strike, right, expiry) -> gamma
        self.spot_fn = spot_fn            # () -> current spot
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
        if not side:
            return
        parsed = _parse(ticker)
        if not parsed:
            return
        root, expiry, right, strike = parsed
        if root not in self.roots:
            return
        gamma = self.gamma_fn(strike, right, expiry)
        if gamma:
            self.ledger.add(strike, size, side, gamma, self.spot_fn())


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
def combine(oi_profile: dict, flow: dict[float, float]) -> dict:
    """Sum the OI baseline and today's flow overlay, recompute levels.
    Returns the same shape as levels.build_profile, plus flow diagnostics."""
    merged = dict(oi_profile.get("by_strike") or {})
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
    })
    return out
