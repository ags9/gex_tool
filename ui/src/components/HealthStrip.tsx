import { parseUtc } from "../api/client";
import type { LiveState } from "../api/useLive";
import { age, clockUtcToEt, money, price } from "./format";

const STALE_SECONDS = 300; // must match reader.STALE_SECONDS

interface Props {
  live: LiveState;
  now: Date;
}

/**
 * Spec §2.1. The one job beyond reporting numbers: make staleness impossible
 * to miss.
 *
 * The age is recomputed from `ts` against a ticking clock rather than read
 * from the last health message, because the health message is itself the
 * thing that stops arriving when something breaks. A strip that showed the
 * server's last-known staleness would freeze at whatever it was when the
 * connection died and keep looking fine — which is the exact failure this
 * guards against.
 */
export function HealthStrip({ live, now }: Props) {
  const { poll, health, conn } = live;

  const pollTs = poll ? parseUtc(poll.ts) : null;
  const ageSeconds = pollTs ? (now.getTime() - pollTs.getTime()) / 1000 : null;

  const marketHours = health?.market_hours ?? false;
  // Trust our own clock over the last health message: see docstring.
  const stale =
    ageSeconds === null || (marketHours && ageSeconds > STALE_SECONDS);
  const amber = stale || health?.ok === false;

  const tone = amber
    ? "border-amber-500/60 bg-amber-950/40 text-amber-200"
    : "border-neutral-800 bg-neutral-900/60 text-neutral-300";

  // NULL is not zero: the overlay being off is a different fact from no flow.
  const overlayOff = poll ? poll.oi_net === null || poll.flow_net === null : true;

  const feedLabel =
    conn === "live" && poll?.feed_connected
      ? "feed live"
      : conn === "live" && poll?.feed_connected === false
        ? "feed down"
        : conn === "live" && poll?.feed_connected === null
          ? "feed off"
          : conn === "reconnecting"
            ? "reconnecting"
            : conn === "connecting"
              ? "connecting"
              : "off";

  const feedTone =
    feedLabel === "feed live"
      ? "text-emerald-400"
      : feedLabel === "feed off"
        ? "text-neutral-500"
        : "text-amber-400";

  return (
    <header
      className={`flex flex-wrap items-center gap-x-6 gap-y-2 border-b px-4 py-3 text-sm ${tone}`}
    >
      {amber && (
        <span className="rounded bg-amber-500/20 px-2 py-0.5 font-semibold tracking-wide text-amber-200">
          {ageSeconds === null
            ? "NO DATA — engine has not polled"
            : `STALE — last poll ${age(ageSeconds)} ago`}
        </span>
      )}

      <Field label="SPX" value={price(poll?.spot)} strong />
      <Field label="net" value={money(poll?.net_gex)} strong />

      <span className="flex items-baseline gap-2">
        <span className="text-neutral-500">OI</span>
        <span>{overlayOff ? "—" : money(poll?.oi_net)}</span>
        <span className="text-neutral-600">·</span>
        <span className="text-neutral-500">flow</span>
        <span>{overlayOff ? "—" : money(poll?.flow_net)}</span>
        {overlayOff && (
          <span className="text-xs text-neutral-600">(overlay off)</span>
        )}
      </span>

      <span className={feedTone}>
        {feedLabel}
        {poll?.feed_trades != null && poll.feed_trades > 0 && (
          <span className="ml-1 text-neutral-500">
            {poll.feed_trades.toLocaleString()} prints
          </span>
        )}
      </span>

      <span className="ml-auto flex items-baseline gap-3 text-neutral-400">
        {!marketHours && (
          <span className="text-xs text-neutral-600">market closed</span>
        )}
        <span>
          as of{" "}
          <span className="text-neutral-200">
            {pollTs ? clockUtcToEt(pollTs) : "—"}
          </span>{" "}
          ET
        </span>
        <span className="text-neutral-500">
          {ageSeconds === null ? "" : `${age(ageSeconds)} ago`}
        </span>
      </span>
    </header>
  );
}

function Field({
  label,
  value,
  strong,
}: {
  label: string;
  value: string;
  strong?: boolean;
}) {
  return (
    <span className="flex items-baseline gap-2">
      <span className="text-neutral-500">{label}</span>
      <span className={strong ? "text-base font-semibold text-neutral-100" : ""}>
        {value}
      </span>
    </span>
  );
}
