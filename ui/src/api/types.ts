// Shapes mirror the tables in gexbot/state.py and the responses in
// gexbot/api/app.py. Kept hand-written rather than generated: the schema is
// small and stable, and the field comments are worth more than the codegen.

export interface Poll {
  poll_id: number;
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
}

export interface Strike {
  strike: number;
  gex: number;
  oi_gex: number | null;      // null = overlay off; 0 = measured as zero
  flow_gex: number | null;
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

/** /api/session/latest — a Poll plus its profile and any open position. */
export interface LatestPoll extends Poll {
  strikes: Strike[];
  position: ShadowTrade | null;
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
  | { type: "poll"; data: Poll & { strikes: Strike[] } }
  | { type: "alert"; data: Alert }
  | { type: "trade"; data: ShadowTrade }
  | { type: "health"; data: Health };
