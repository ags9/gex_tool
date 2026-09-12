# gexbot UI

Local operator dashboard. Read-only: it renders what the engine recorded and
changes nothing (Phase 2 §4.3).

## Run

```bash
python -m gexbot api      # state API on 127.0.0.1:8742
cd ui && npm install && npm run dev
```

Vite serves on `GEX_DASHBOARD_PORT` (8741) and proxies `/api` and `/ws` to
`GEX_API_PORT` (8742). Both bind loopback only — this box holds brokerage
credentials, so remote viewing means a tunnel, never a bind address.

Port already in use? `explore.py` (the Streamlit results explorer) also
claims 8741. Stop it, or run the dev server elsewhere for the moment:

```bash
GEX_DASHBOARD_PORT=8743 npm run dev
```

`explore.py` is retired in build step 5, once the Research screen reaches
parity.

## Layout

```
src/api/types.ts     shapes mirroring gexbot/state.py tables
src/api/client.ts    typed fetch + parseUtc()  ← read its docstring
src/api/useLive.ts   the /ws/live socket, reconnecting, snapshot-first
src/components/      HealthStrip and formatters
```

## Two things to know before editing

**`parseUtc`, not `new Date`.** `poll_snapshot.ts` is a naive UTC timestamp
and arrives without an offset. `new Date()` reads an offset-less string as
*local* time, which silently shifts every timestamp by the machine's offset
and makes a fresh poll look hours stale. Same class of bug as the
replay/parity mess in CLAUDE.md §9.

**NULL is not zero.** `oi_net`, `flow_net`, `oi_gex`, `flow_gex` are NULL
when the flow overlay was off and `0.0` when it ran and measured nothing.
Render NULL as a gap or an em dash — never as a zero line (spec §6.3).
