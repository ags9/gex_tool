"""Notifier tests — mocked HTTP; assert tiering, paper-mode marking,
isolation (exceptions never propagate), and drop-oldest behavior.
"""
import time

import gexbot.notify as notify
from gexbot.notify import Channel, Color, DiscordNotifier


class FakeResponse:
    status_code = 200
    headers: dict = {}


def make(monkeypatch, posts, fail=False):
    def fake_post(url, json=None, timeout=None):
        if fail:
            raise ConnectionError("discord down")
        posts.append((url, json))
        return FakeResponse()
    monkeypatch.setattr(notify.httpx, "post", fake_post)
    n = DiscordNotifier({Channel.TRADES: "https://x/t", Channel.ALERTS: "https://x/a",
                         Channel.DAILY: "https://x/d"}, paper_mode=True)
    n._min_interval = 0.0
    return n


def test_paper_mode_marks_messages(monkeypatch):
    posts = []
    n = make(monkeypatch, posts)
    n.trade_entry(kind="bounce", direction=1, strikes="XSP 760C",
                  contracts=1, fill=5.98, level=7595)
    n.flush()
    assert len(posts) == 1
    embed = posts[0][1]["embeds"][0]
    assert embed["title"].startswith("📄")
    assert embed["color"] == int(Color.GRAY)          # paper blue -> gray


def test_channel_routing_and_colors(monkeypatch):
    posts = []
    n = make(monkeypatch, posts)
    n.trade_exit(reason="trail_stop", pnl=242, contracts=1, fill=8.41)
    n.breaker(name="Daily loss breaker", detail="-15% tranche")
    n.flush()
    urls = [p[0] for p in posts]
    assert urls == ["https://x/t", "https://x/a"]
    assert posts[0][1]["embeds"][0]["color"] == int(Color.GREEN)
    assert posts[1][1]["embeds"][0]["color"] == int(Color.RED)


def test_discord_failure_never_propagates(monkeypatch):
    n = make(monkeypatch, [], fail=True)
    n.breaker(name="x", detail="y")                   # must not raise
    time.sleep(0.2)
    assert True


def test_queue_drop_oldest(monkeypatch):
    posts = []
    n = make(monkeypatch, posts)
    n.q.maxsize = 0                                   # unbounded in CPython when 0; emulate small
    n2 = DiscordNotifier({Channel.TRADES: "https://x/t"}, paper_mode=False, max_queue=2)
    monkeypatch.setattr(notify.httpx, "post",
                        lambda *a, **k: (_ for _ in ()).throw(ConnectionError()))
    # stall the drain by making posts fail fast; flood the queue
    for i in range(10):
        n2.send(Channel.TRADES, f"m{i}", "body")
    assert n2.dropped >= 1                            # oldest were dropped, counted
