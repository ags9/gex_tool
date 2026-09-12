// Shapes mirror the tables in gexbot/state.py and the responses in
// gexbot/api/app.py. Kept hand-written rather than generated: the schema is
// small and stable, and the field comments are worth more than the codegen.

export interface Poll {
  poll_id: number;
  underlying: string;
  ts: string;                 // NAIVE UTC — see parseUtc()
  session_date: string;
  minute_of_day: number;
  spot: number;
  net_gex: number;
  oi_net: number | null;      // null = flow overlay was OFF, not zero
  flow_net: number | null;
  flip: number | null;
  put_wall: number | null;
  call_wall: number | null;
  max_accel: number | null;
  max_magnet: number | null;
  first_pos_above: number | null;
  expiries: string;
  model: string;
  per_point: boolean;
  feed_connected: boolean | null;
  feed_trades: number | null;
  feed_contracts: number | null;
  poll_ms: number | null;
  net_dex: number | null;
  spy_ratio: number | null;
}

export interface Expiry {
  poll_id: number;
  expiry: string;
  gamma: number;
  delta: number;
  oi: number;
  put_call_oi: number | null;
  opex: "monthly" | "quarterly" | null;
  pct_of_total: number | null;
  pct_remaining_after: number | null;
}

export interface Strike {
  strike: number;
  gex: number;
  oi_gex: number | null;      // null = overlay off; 0 = measured as zero
  flow_gex: number | null;
  dex: number | null;
  /** From levels.level_label, server-side. Never derived here (spec §0). */
  label: "SUPPORT" | "RESISTANCE" | "TRAPDOOR" | "LAUNCHPAD";
  /** Unsigned day volume. Activity, not positioning. null = not recorded. */
  volume: number | null;
}

export interface ShadowTrade {
  trade_id: number;
  session_date: string;
  direction: number;
  strike: number;
  expiry: string;
  contracts: number;
  trigger: string;
  level: number | null;
  entry_ts: string;
  entry_minute: number;
  entry_spot: number;
  entry_premium: number;
  exit_ts: string | null;
  exit_minute: number | null;
  exit_spot: number | null;
  exit_premium: number | null;
  exit_reason: string | null;
  pnl: number | null;
}

export interface Alert {
  alert_id: number;
  poll_id: number | null;
  ts: string;
  channel: string;
  kind: string;
  title: string;
  body: string;
  spot: number | null;
  strike: number | null;
}

/** A level annotated by the ENGINE. `label` comes from levels.level_label —
 *  the UI must never derive it (spec §0). */
export interface Level {
  kind:
    | "flip"
    | "put_wall"
    | "call_wall"
    | "max_accel"
    | "max_magnet"
    | "first_pos_above";
  strike: number;
  gex: number;
  label: "SUPPORT" | "RESISTANCE" | "TRAPDOOR" | "LAUNCHPAD";
  distance_pts: number;
  distance_pct: number;
}

export interface Regime {
  state: "POSITIVE" | "NEGATIVE" | "NEUTRAL";
  called: boolean;
  session_peak_abs_net: number;
  description: string;
}

/** Everything the right rail needs, computed server-side so the dashboard
 *  cannot disagree with the Discord alert describing the same instant. */
export interface PollContext {
  levels: Level[];
  regime: Regime;
  position_text: string;
  position: ShadowTrade | null;
}

/** /api/session/latest — a Poll plus its profile and any open position. */
export interface LatestPoll extends Poll {
  strikes: Strike[];
  position: ShadowTrade | null;
  context: PollContext;
  as_of: string;
}

export interface Health {
  ok: boolean;
  engine_last_poll_ts: string | null;
  engine_stale_seconds: number | null;
  feed_connected: boolean | null;
  feed_trades: number | null;
  feed_contracts: number | null;
  write_errors: number | null;
  market_hours: boolean;
  db: string;
  version: string;
  as_of: string | null;
  detail?: string;
}

export type LiveMessage =
  | { type: "snapshot"; data: LatestPoll | null }
  | { type: "poll"; data: LatestPoll }
  | { type: "alert"; data: Alert }
  | { type: "trade"; data: ShadowTrade }
  | { type: "health"; data: Health }
  | { type: "prints"; data: { prints: TapePrint[]; stats: TapeStats } };

export interface Premium {
  poll_id: number;
  ts: string;
  session_date: string;
  minute_of_day: number;
  call_bought: number;
  call_sold: number;
  put_bought: number;
  put_sold: number;
  trades_counted: number;
  unclassified: number;
}

export interface Heatmap {
  minutes: number[];
  strikes: number[];
  /** [minute, strike, gex] — long form: strikes drift in and out of the
   *  window, so a dense matrix would have to invent values for the holes. */
  cells: [number, number, number][];
}

export interface SessionSummary {
  session_date: string;
  polls: number;
  trades: number;
  shadow_pnl: number;
  alerts: number;
  first_minute: number;
  last_minute: number;
}

export interface BundleSummary {
  name: string;
  has_gates: boolean;
  modified: string;
  gate_params?: Record<string, number> | null;
  go_live_eligible?: boolean;
}

export interface MarkProvenance {
  file: number;
  rest: number;
  model_fallback: number;
  total: number;
  fallback_pct: number | null;
}

export interface Bundle {
  name: string;
  gates: Record<string, { passed: boolean; detail: string }> | null;
  gate_params: Record<string, number> | null;
  /** null = the bundle predates provenance recording. Not 0% fallback. */
  mark_provenance: MarkProvenance | null;
  summary_md: string | null;
  days: Record<string, unknown>[];
  trades: Record<string, unknown>[];
}

/** One classified print (spec §15.2). */
export interface TapePrint {
  seq: number;
  underlying: string;
  ts: string;
  minute_of_day: number;
  ticker: string;
  root: string;
  expiry: string;
  strike: number;
  option_right: string;
  price: number;
  size: number;
  side: number;            // +1 buy, -1 sell, 0 unclassified
  premium: number;
  /** Diagnostic: 0 means the gamma lookup missed and this print moved nothing. */
  gamma_used: number;
  /** Diagnostic: the signed contribution the ledger actually recorded. */
  dealer_gamma_delta: number;
}

export interface TapeStats {
  prints_seen: number;
  contracts_seen: number;
  unclassified: number;
  unclassified_pct: number | null;
  gamma_misses: number;
  gamma_miss_pct: number | null;
  block_threshold: number | null;
}

export interface ChainQuote {
  bid: number | null;
  ask: number | null;
  mid: number | null;
  spread: number | null;
}

export interface ChainRow {
  strike: number;
  spx_equivalent: number;
  gex: number | null;
  /** "own" = this symbol's chain. "spx_map" = XSP publishes no greeks, so the
   *  SPX map's gamma at the equivalent level is shown instead. */
  gex_source: "own" | "spx_map" | "unavailable";
  call: ChainQuote | null;
  put: ChainQuote | null;
  atm: boolean;
}

export interface ChainView {
  symbol: "SPX" | "XSP";
  spot: number | null;
  spx_equivalent_spot?: number;
  expiry: string | null;
  dte: number | null;
  rows: ChainRow[];
  gex_source: "own" | "spx_map" | "unavailable";
  levels: Record<string, number | null>;
  net_gex: number | null;
  detail?: string;
}
