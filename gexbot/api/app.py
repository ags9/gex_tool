"""Read-only API over the state store (Phase 2 spec §4).

Three constraints shape this file, and all three are structural rather than
conventional:

**Read-only.** There is no write path. No endpoint starts, stops, or
configures the engine, places a trade, or touches a parameter. The HALT
control the monitoring spec contemplates (§14.4) is explicitly not here.
`BundleReader` reads finished backtests and cannot run one — putting a
strategy evaluation behind a URL is exactly the casual re-running the
pre-registration exists to prevent.

**Localhost.** `run_api` binds 127.0.0.1 and takes no host argument, so
there is no flag that exposes a box holding brokerage credentials to the
network. Remote viewing means a tunnel, not a bind address.

**Staleness is always visible.** Every response carries the timestamp of the
poll it reflects, so a UI cannot silently render old numbers as current. See
`as_of_middleware` for how that is applied to list responses.

The engine never pushes into this process. The WebSocket polls the database
instead (spec §4.2), which keeps a slow or wedged UI from ever stalling the
trading loop — the two processes share a file, not a call stack.
"""
from __future__ import annotations

import asyncio
import datetime as dt

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware

from ..config import settings
from .reader import BundleReader, StateReader

WS_POLL_SECONDS = 1.0
AS_OF_HEADER = "X-As-Of"


def create_app(db_path=None, results_root=None) -> FastAPI:
    reader = StateReader(db_path)
    bundles = BundleReader(results_root)

    app = FastAPI(title="gexbot", version="0.1.0",
                  description="Read-only view of the live engine's state store.")
    app.state.reader = reader
    app.state.bundles = bundles

    origins = [f"http://127.0.0.1:{settings.gex_dashboard_port}",
               f"http://localhost:{settings.gex_dashboard_port}"]
    app.add_middleware(CORSMiddleware, allow_origins=origins,
                       allow_credentials=False, allow_methods=["GET"],
                       allow_headers=["*"], expose_headers=[AS_OF_HEADER])

    @app.middleware("http")
    async def as_of_middleware(request, call_next):
        """Stamp every response with the poll it reflects.

        Object responses carry `as_of` in the body, but the spec's list
        endpoints return bare arrays, which have nowhere to put it. The
        header covers those without changing the documented shapes; it is
        CORS-exposed so browser code can read it.
        """
        response = await call_next(request)
        try:
            h = await asyncio.to_thread(reader.health)
            response.headers[AS_OF_HEADER] = str(h.get("as_of") or "")
        except Exception:                      # never fail a response over this
            pass
        return response

    def _date(value: str) -> dt.date:
        try:
            return dt.date.fromisoformat(value)
        except ValueError:
            raise HTTPException(400, f"bad date {value!r} — expected YYYY-MM-DD")

    # ── health ───────────────────────────────────────────────────────
    @app.get("/api/health")
    async def health():
        return await asyncio.to_thread(reader.health)

    # ── session ──────────────────────────────────────────────────────
    @app.get("/api/underlyings")
    async def underlyings():
        return await asyncio.to_thread(reader.underlyings)

    @app.get("/api/session/latest")
    async def session_latest(underlying: str | None = Query(None)):
        poll = await asyncio.to_thread(reader.latest, underlying)
        if poll is None:
            raise HTTPException(404, "no polls recorded yet")
        poll["as_of"] = poll["ts"]
        return poll

    @app.get("/api/session/{date}/polls")
    async def session_polls(date: str,
                            from_minute: int | None = Query(None, ge=0, le=1440),
                            to_minute: int | None = Query(None, ge=0, le=1440),
                            underlying: str | None = Query(None)):
        return await asyncio.to_thread(reader.polls, _date(date),
                                       from_minute=from_minute,
                                       to_minute=to_minute,
                                       underlying=underlying)

    @app.get("/api/session/{date}/profile")
    async def session_profile(date: str,
                              minute: int | None = Query(None, ge=0, le=1440),
                              underlying: str | None = Query(None)):
        poll = await asyncio.to_thread(reader.profile_at, _date(date), minute,
                                       underlying)
        if poll is None:
            raise HTTPException(404, f"no poll on {date} at or before "
                                     f"minute {minute}")
        poll["as_of"] = poll["ts"]
        return poll

    @app.get("/api/tape")
    async def tape(underlying: str | None = Query(None),
                   limit: int = Query(200, ge=1, le=2000),
                   min_size: int | None = Query(None, ge=1),
                   strike: float | None = Query(None),
                   right: str | None = Query(None, pattern="^[CPcp]$"),
                   side: int | None = Query(None, ge=-1, le=1)):
        prints = await asyncio.to_thread(
            reader.tape, underlying, limit=limit, min_size=min_size,
            strike=strike, right=right, side=side)
        stats = await asyncio.to_thread(reader.tape_stats, underlying)
        return {"prints": prints, "stats": stats}

    @app.get("/api/session/expiry")
    async def session_expiry(poll_id: int = Query(...)):
        return await asyncio.to_thread(reader.expiries, poll_id)

    @app.get("/api/session/narrate")
    async def session_narrate(underlying: str | None = Query(None),
                              poll_id: int | None = Query(None),
                              refresh: bool = Query(False)):
        """Spec §13. A failure here returns narration: null and the UI shows
        the numbers without prose — it never blocks anything."""
        poll = await asyncio.to_thread(
            reader.latest, underlying) if poll_id is None else \
            await asyncio.to_thread(reader.poll_by_id, poll_id)
        if poll is None:
            raise HTTPException(404, "no poll to narrate")

        stored = await asyncio.to_thread(reader.narration, poll["poll_id"])
        if stored and not refresh:
            return {"poll_id": poll["poll_id"], "as_of": poll["ts"],
                    "narration": stored["text"], "model": stored["model"],
                    "cached": True}

        from ..narrate import narrate
        from ..state import StateStore
        prev = await asyncio.to_thread(reader.previous_poll, poll["poll_id"],
                                       poll.get("underlying") or "I:SPX")
        exps = await asyncio.to_thread(reader.expiries, poll["poll_id"])
        result = await asyncio.to_thread(narrate, poll, poll["context"], prev, exps)
        # Only model prose is cached. A templated fallback is cheap to
        # recompute and should not fossilise into the record as though the
        # model produced it.
        if result.ok and result.text:
            await asyncio.to_thread(
                StateStore(reader.path).write_narration,
                poll["poll_id"], result.text, result.model)
        return {"poll_id": poll["poll_id"], "as_of": poll["ts"],
                "narration": result.text, "model": result.model,
                "source": result.source,
                "violations": result.violations, "cached": False}

    @app.get("/api/session/{date}/premium")
    async def session_premium(date: str):
        return await asyncio.to_thread(reader.premium, _date(date))

    @app.get("/api/session/{date}/heatmap")
    async def session_heatmap(date: str, underlying: str | None = Query(None)):
        return await asyncio.to_thread(reader.strike_matrix, _date(date),
                                       underlying)

    @app.get("/api/session/{date}/alerts")
    async def session_alerts(date: str, underlying: str | None = Query(None)):
        return await asyncio.to_thread(reader.alerts, _date(date), underlying)

    @app.get("/api/session/{date}/trades")
    async def session_trades(date: str, underlying: str | None = Query(None)):
        return await asyncio.to_thread(reader.trades, _date(date), underlying)

    @app.get("/api/sessions")
    async def sessions(limit: int = Query(30, ge=1, le=500)):
        return await asyncio.to_thread(reader.sessions, limit)

    # ── backtest bundles ─────────────────────────────────────────────
    @app.get("/api/backtests")
    async def backtests():
        return await asyncio.to_thread(bundles.list)

    @app.get("/api/backtests/{name}")
    async def backtest(name: str):
        b = await asyncio.to_thread(bundles.get, name)
        if b is None:
            raise HTTPException(404, f"no bundle {name!r}")
        return b

    # ── live websocket ───────────────────────────────────────────────
    @app.websocket("/ws/live")
    async def ws_live(ws: WebSocket):
        await ws.accept()
        underlying = ws.query_params.get("underlying") or None
        last_poll, last_alert = await asyncio.to_thread(reader.max_ids, underlying)
        last_seq = await asyncio.to_thread(reader.max_tape_seq, underlying)
        trade_state: dict[int, object] = {}

        snapshot = await asyncio.to_thread(reader.latest, underlying)
        if snapshot is not None:
            snapshot["as_of"] = snapshot["ts"]
        await ws.send_json(jsonable_encoder(
            {"type": "snapshot", "data": snapshot}))

        # seed the trade watermark so reconnecting does not replay the day
        for t in await asyncio.to_thread(reader.trades_changed_after, 0):
            trade_state[t["trade_id"]] = t["exit_ts"]

        try:
            while True:
                await asyncio.sleep(WS_POLL_SECONDS)

                # one connection per tick, not four — see reader.live_tick
                tick = await asyncio.to_thread(reader.live_tick,
                                               last_poll, last_alert, underlying)

                for poll in tick["polls"]:
                    last_poll = max(last_poll, poll["poll_id"])
                    await ws.send_json(jsonable_encoder(
                        {"type": "poll", "data": poll}))

                for alert in tick["alerts"]:
                    last_alert = max(last_alert, alert["alert_id"])
                    await ws.send_json(jsonable_encoder(
                        {"type": "alert", "data": alert}))

                # a close updates a row rather than adding one, so an id
                # watermark alone would never report an exit
                for t in tick["trades"]:
                    tid = t["trade_id"]
                    if tid not in trade_state or trade_state[tid] != t["exit_ts"]:
                        trade_state[tid] = t["exit_ts"]
                        await ws.send_json(jsonable_encoder(
                            {"type": "trade", "data": t}))

                # Prints ride the existing 1 s push as one batch. A message
                # per print would flood the browser on SPX.
                new_prints = await asyncio.to_thread(
                    reader.tape, underlying, limit=500, after_seq=last_seq)
                if new_prints:
                    last_seq = max(p["seq"] for p in new_prints)
                    stats = await asyncio.to_thread(reader.tape_stats, underlying)
                    await ws.send_json(jsonable_encoder(
                        {"type": "prints",
                         "data": {"prints": new_prints, "stats": stats}}))

                await ws.send_json(jsonable_encoder(
                    {"type": "health",
                     "data": await asyncio.to_thread(reader.health)}))
        except WebSocketDisconnect:
            return
        except RuntimeError:
            return                              # socket closed mid-send

    return app


def run_api(port: int | None = None, db_path=None) -> None:
    """Serve on 127.0.0.1 only. There is deliberately no host parameter."""
    import uvicorn
    from dotenv import load_dotenv

    # The other subcommands load .env in their handlers; the API needs it too
    # now that narration reads ANTHROPIC_API_KEY.
    load_dotenv()

    from rich.console import Console
    console = Console()
    port = port or settings.gex_api_port
    console.print(f"[bold]gexbot API[/bold] · http://127.0.0.1:{port}/api/health "
                  f"· docs at /docs")
    console.print("[dim]read-only · localhost only · engine is never called "
                  "from here[/dim]")
    uvicorn.run(create_app(db_path), host="127.0.0.1", port=port,
                log_level="info")
