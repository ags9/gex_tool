# Phase 1 + 2 Spec — State Store & API

**For:** Claude Code, implementing against `/Volumes/Main Drive/development/GexDev`
**Prerequisites:** read `CLAUDE.md` first. The constraints there bind this work.
**Scope:** the persistence layer the engine writes, and the read API the UI
consumes. No UI in this phase. No strategy changes of any kind.

---

## 0. Why this exists

`watch.py` currently keeps state in a single JSON file that is overwritten
each poll. That is enough to prevent duplicate alerts and nothing else:
there is no history, so nothing can chart the day, compare live decisions
against a later replay, or answer "what did the map look like at 11:40?"

Phase 1 makes every poll durable. Phase 2 exposes it. Both are read-only
with respect to strategy: **no code in this phase may alter entry, exit,
sizing, or discipline behaviour**, and nothing here may write to the
parameter config (CLAUDE.md §18 rule).

---

## 1. Storage choice

DuckDB file at `{GEX_DATA_ROOT}/state.duckdb`.

**Single-writer rule.** The engine process is the only writer. It opens a
connection per write operation and closes it (the pattern already used in
`manifest.py` — do not hold a handle open). Readers (API, CLI, notebooks)
open `read_only=True`. A held write connection blocks all readers; this bit
us before.

`watch_state.json` remains as-is for crash recovery of the in-flight shadow
position. Do not migrate it into DuckDB — its job is different (small,
atomic, human-readable) and it is already working.

---

## 2. Schema

All timestamps are stored UTC as `TIMESTAMP`; all *session* times are
stored additionally as `minute_of_day` (INTEGER, ET) using `gexbot.clock`.
Never recompute ET from stored UTC in SQL — use the clock module.

```sql
-- one row per poll
CREATE TABLE IF NOT EXISTS poll_snapshot (
    poll_id          BIGINT PRIMARY KEY,     -- monotonic, see §2.1
    ts               TIMESTAMP NOT NULL,
    session_date     DATE NOT NULL,
    minute_of_day    INTEGER NOT NULL,
    spot             DOUBLE NOT NULL,
    net_gex          DOUBLE NOT NULL,
    oi_net           DOUBLE,                 -- null when flow overlay is off
    flow_net         DOUBLE,
    flip             DOUBLE,
    put_wall         DOUBLE,
    call_wall        DOUBLE,
    max_accel        DOUBLE,
    max_magnet       DOUBLE,
    first_pos_above  DOUBLE,
    expiries         VARCHAR,                -- comma-joined, as aggregated
    model            VARCHAR NOT NULL,       -- 'naive' | 'short_all'
    per_point        BOOLEAN NOT NULL,
    feed_connected   BOOLEAN,
    feed_trades      BIGINT,                 -- cumulative today
    feed_contracts   BIGINT,
    poll_ms          INTEGER                 -- wall time for this poll
);

-- the gamma profile for that poll, long-form
CREATE TABLE IF NOT EXISTS poll_strike (
    poll_id          BIGINT NOT NULL,
    strike           DOUBLE NOT NULL,
    gex              DOUBLE NOT NULL,        -- combined (OI + flow)
    oi_gex           DOUBLE,                 -- baseline component
    flow_gex         DOUBLE,                 -- today's flow component
    PRIMARY KEY (poll_id, strike)
);

-- every alert actually sent
CREATE TABLE IF NOT EXISTS alert_log (
    alert_id         BIGINT PRIMARY KEY,
    poll_id          BIGINT,
    ts               TIMESTAMP NOT NULL,
    channel          VARCHAR NOT NULL,       -- trades | alerts | daily
    kind             VARCHAR NOT NULL,       -- regime_flip | flip_cross |
                                             -- proximity | map | shadow_entry |
                                             -- shadow_exit | digest | warning
    title            VARCHAR NOT NULL,
    body             VARCHAR NOT NULL,
    spot             DOUBLE,
    strike           DOUBLE                  -- when the alert concerns one
);

-- shadow trades, entry and exit in one row (exit cols null while open)
CREATE TABLE IF NOT EXISTS shadow_trade (
    trade_id         BIGINT PRIMARY KEY,
    session_date     DATE NOT NULL,
    direction        INTEGER NOT NULL,       -- +1 call, -1 put
    strike           DOUBLE NOT NULL,
    expiry           DATE NOT NULL,
    contracts        INTEGER NOT NULL,
    trigger          VARCHAR NOT NULL,
    level            DOUBLE,
    entry_ts         TIMESTAMP NOT NULL,
    entry_minute     INTEGER NOT NULL,
    entry_spot       DOUBLE NOT NULL,
    entry_premium    DOUBLE NOT NULL,
    exit_ts          TIMESTAMP,
    exit_minute      INTEGER,
    exit_spot        DOUBLE,
    exit_premium     DOUBLE,
    exit_reason      VARCHAR,
    pnl              DOUBLE
);
```

### 2.1 poll_id
`poll_id = int(ts.timestamp() * 1000)`. Monotonic, collision-free at our
cadence, and directly sortable. Same scheme for `alert_id` and `trade_id`
(use a counter suffix if two land in the same millisecond).

### 2.2 Retention
None for now — a poll every 3 minutes is ~130 rows/day plus ~40 strikes
each. Years of this is trivial. Do not add pruning logic.

### 2.3 Write failure policy
A failed state write must **never** interrupt the engine or suppress an
alert. Wrap writes, log the exception, increment a counter exposed at
`/api/health`, continue. Losing a poll row is acceptable; missing a
breaker alert is not.

---

## 3. Phase 1 deliverables

1. `gexbot/state.py` — schema creation (idempotent), and typed writers:
   `write_poll(profile, feed_stats, poll_ms) -> poll_id`,
   `write_alert(...)`, `open_shadow_trade(...)`, `close_shadow_trade(...)`.
   Readers live in the API, not here.
2. `watch.py` calls those writers. Every alert that goes to Discord is
   also written to `alert_log` — the notifier and the store must never
   disagree, so write from the same call site.
3. Backfill nothing. History starts now.
4. Tests: schema idempotency, a poll round-trip, an alert round-trip, a
   shadow trade open→close round-trip, and a test asserting that a write
   exception does not propagate out of the engine call.

---

## 4. Phase 2 — API (FastAPI, port `GEX_API_PORT`, default 8742)

Bind `127.0.0.1` only. No auth (localhost). CORS allows
`http://127.0.0.1:{GEX_DASHBOARD_PORT}` and `http://localhost:{...}`.

### 4.1 REST

```
GET /api/health
    -> { ok, engine_last_poll_ts, engine_stale_seconds, feed_connected,
         feed_trades, feed_contracts, write_errors, version }

GET /api/session/latest
    -> the most recent poll_snapshot as an object, plus:
       { strikes: [{strike, gex, oi_gex, flow_gex}, ...],
         position: {...} | null }     # current shadow position

GET /api/session/{date}/polls?from_minute=&to_minute=
    -> [ poll_snapshot, ... ]         # no strike detail, for charting spot/net

GET /api/session/{date}/profile?minute=
    -> the strike profile at (or nearest before) that minute
       — this is what powers "what did the map look like at 11:40?"

GET /api/session/{date}/alerts        -> [ alert_log rows ]
GET /api/session/{date}/trades        -> [ shadow_trade rows ]
GET /api/sessions?limit=30            -> [ {session_date, polls, alerts,
                                            trades, shadow_pnl} ]

GET /api/backtests                    -> [ {bundle_name, start, end, ...} ]
GET /api/backtests/{name}             -> gates.json + day/trade summaries
```

Backtest endpoints read the existing results bundles on disk
(`{GEX_DATA_ROOT}/results/*`). Do not re-run anything from the API.

### 4.2 WebSocket

```
WS /ws/live
```
On connect: send one `snapshot` message (same shape as
`/api/session/latest`). Thereafter push on each new poll:

```json
{ "type": "poll",   "data": { ...poll_snapshot..., "strikes": [...] } }
{ "type": "alert",  "data": { ...alert_log row... } }
{ "type": "trade",  "data": { ...shadow_trade row... } }
{ "type": "health", "data": { ...health object... } }
```

Implementation: the API polls DuckDB for `poll_id > last_seen` on a short
interval (1 s) and fans out. **Do not** have the engine push into the API —
that would couple the two processes and let a slow UI stall the engine.

### 4.3 Non-negotiables

- The API is **read-only**. No endpoint may start, stop, or configure the
  engine, place a trade, or alter parameters. The one exception permitted
  later is a HALT control (monitoring spec §14.4) — not in this phase.
- Every response carries `as_of` (the poll ts it reflects) so the UI can
  show staleness rather than silently displaying old numbers.
- If the engine has not polled in > 5 minutes during market hours,
  `/api/health` reports `ok: false`. The UI is expected to surface that.

---

## 5. Acceptance

- `python -m gexbot watch --once` writes exactly one `poll_snapshot` row,
  its strikes, and any alerts it sent.
- `python -m gexbot api` (new subcommand) serves the endpoints above;
  `curl localhost:8742/api/health` returns sane JSON with the engine down.
- Running the engine and API simultaneously produces no DuckDB lock errors
  over a 30-minute soak.
- Tests green (currently 55).
- No file under `gexbot/` that implements entries, exits, discipline,
  sizing, or gates is modified by this work. `git diff --stat` should show
  changes confined to `state.py`, `watch.py`, `api/`, `__main__.py`, tests.

---

## 6. Out of scope (do not build yet)

React app, charts, styling, Docker, auth, remote access, nightly analysis
(§18), any strategy or parameter change, any new alert type.
