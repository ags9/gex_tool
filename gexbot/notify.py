"""Discord alerting (monitoring spec §13) — three tiered webhooks.

Design rules encoded here:
- Fire-and-forget: a background thread drains a queue; Discord being down,
  slow, or rate-limiting can NEVER touch the trading path. Queue full ->
  drop-oldest, count the drops, mention them in the next daily digest.
- Tiering: trades (quiet), alerts (@ping-worthy), daily (digest). Paper-mode
  messages are visually distinct (📄 prefix + muted color) so a phone glance
  can never confuse simulated with real.
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from enum import Enum

import httpx


class Channel(str, Enum):
    TRADES = "trades"
    ALERTS = "alerts"
    DAILY = "daily"


class Color(int, Enum):
    GREEN = 0x2ECC71     # fills at profit, go signals
    RED = 0xE74C3C       # stops, breakers, failures
    AMBER = 0xF39C12     # warnings, lockouts
    BLUE = 0x3498DB      # info, entries
    GRAY = 0x95A5A6      # paper mode


@dataclass
class Message:
    channel: Channel
    title: str
    body: str
    color: int
    fields: list[tuple[str, str]] | None = None


class DiscordNotifier:
    def __init__(self, webhooks: dict[Channel, str], *, paper_mode: bool = True,
                 max_queue: int = 500):
        self.webhooks = {c: u for c, u in webhooks.items() if u}
        self.paper_mode = paper_mode
        self.q: queue.Queue[Message] = queue.Queue(maxsize=max_queue)
        self.dropped = 0
        self._last_post = 0.0
        self._min_interval = 60.0 / 25.0          # ≤25 msgs/min/thread
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    # ── public api ───────────────────────────────────────────────────
    def send(self, channel: Channel, title: str, body: str,
             color: int = Color.BLUE, fields: list[tuple[str, str]] | None = None) -> None:
        if self.paper_mode:
            title = f"📄 {title}"
            color = Color.GRAY if color == Color.BLUE else color
        try:
            self.q.put_nowait(Message(channel, title, body, int(color), fields))
        except queue.Full:
            try:
                self.q.get_nowait()               # drop-oldest
            except queue.Empty:
                pass
            self.dropped += 1
            try:
                self.q.put_nowait(Message(channel, title, body, int(color), fields))
            except queue.Full:
                self.dropped += 1

    # convenience wrappers used by the engine
    def trade_entry(self, *, kind: str, direction: int, strikes: str,
                    contracts: int, fill: float, level: float) -> None:
        side = "CALL" if direction > 0 else "PUT"
        self.send(Channel.TRADES, f"Entry — {side} {kind}",
                  f"{strikes} ×{contracts} @ {fill:.2f}",
                  Color.BLUE, [("Level", f"{level:.0f}")])

    def trade_exit(self, *, reason: str, pnl: float, contracts: int,
                   fill: float) -> None:
        self.send(Channel.TRADES, f"Exit — {reason}",
                  f"×{contracts} @ {fill:.2f}  P&L ${pnl:+,.0f}",
                  Color.GREEN if pnl >= 0 else Color.RED)

    def breaker(self, *, name: str, detail: str) -> None:
        self.send(Channel.ALERTS, f"⛔ {name}", detail, Color.RED)

    def warning(self, *, title: str, detail: str) -> None:
        self.send(Channel.ALERTS, f"⚠️ {title}", detail, Color.AMBER)

    def daily_digest(self, *, body: str, green_day: bool) -> None:
        note = f"\n(dropped {self.dropped} alert(s) today)" if self.dropped else ""
        self.send(Channel.DAILY, "Daily digest", body + note,
                  Color.GREEN if green_day else Color.RED)
        self.dropped = 0

    # ── worker ───────────────────────────────────────────────────────
    def _drain(self) -> None:
        while True:
            m = self.q.get()
            try:
                url = self.webhooks.get(m.channel)
                if not url:
                    continue
                wait = self._min_interval - (time.monotonic() - self._last_post)
                if wait > 0:
                    time.sleep(wait)
                payload = {"embeds": [{
                    "title": m.title[:256], "description": m.body[:4000],
                    "color": m.color,
                    "fields": [{"name": n[:256], "value": v[:1024], "inline": True}
                               for n, v in (m.fields or [])],
                }]}
                try:
                    r = httpx.post(url, json=payload, timeout=5)
                    if r.status_code == 429:      # rate limited: brief backoff
                        time.sleep(float(r.headers.get("Retry-After", "2")))
                        httpx.post(url, json=payload, timeout=5)
                except Exception:
                    pass                          # never propagate
            finally:
                self._last_post = time.monotonic()
                self.q.task_done()                # marks POST complete, not just dequeue

    def flush(self, timeout: float = 15.0) -> None:
        """Waits until every queued message has actually been POSTED
        (task_done), not merely dequeued — the last message must not die
        with the process. Fixes the vanishing-final-message bug."""
        deadline = time.monotonic() + timeout
        while self.q.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.05)


def from_settings() -> DiscordNotifier:
    import os
    return DiscordNotifier({
        Channel.TRADES: os.getenv("DISCORD_WEBHOOK_TRADES", ""),
        Channel.ALERTS: os.getenv("DISCORD_WEBHOOK_ALERTS", ""),
        Channel.DAILY: os.getenv("DISCORD_WEBHOOK_DAILY", ""),
    }, paper_mode=True)
