import { parseUtc } from "./api/client";
import { useLive, useNow } from "./api/useLive";
import { HealthStrip } from "./components/HealthStrip";
import { age, clockUtcToEt, money, price } from "./components/format";

/**
 * Step 1 of the build order: scaffold, client, socket, health strip.
 *
 * Below the strip is a deliberately plain live-wire panel that exists to
 * prove the socket is delivering — it is replaced by the strike profile in
 * step 2, not kept.
 */
export default function App() {
  const live = useLive();
  const now = useNow();
  const { poll, health, alerts, pollsReceived, conn } = live;

  return (
    <div className="min-h-screen">
      <HealthStrip live={live} now={now} />

      <main className="mx-auto max-w-5xl space-y-6 p-6">
        <section className="rounded-lg border border-neutral-800 bg-neutral-900/40 p-4">
          <h2 className="mb-3 text-xs font-semibold uppercase tracking-widest text-neutral-500">
            Live wire · step 1 proof
          </h2>
          <dl className="grid grid-cols-2 gap-x-8 gap-y-2 text-sm sm:grid-cols-3">
            <Row label="socket" value={conn} />
            <Row label="polls received" value={String(pollsReceived)} />
            <Row
              label="last message"
              value={
                live.lastMessageAt
                  ? `${age((now.getTime() - live.lastMessageAt.getTime()) / 1000)} ago`
                  : "—"
              }
            />
            <Row label="poll_id" value={poll ? String(poll.poll_id) : "—"} />
            <Row label="strikes" value={poll ? String(poll.strikes.length) : "—"} />
            <Row
              label="flow split"
              value={
                poll?.strikes?.length
                  ? poll.strikes.every((s) => s.flow_gex === null)
                    ? "NULL (overlay off)"
                    : `${poll.strikes.filter((s) => s.flow_gex !== null).length}/${poll.strikes.length} populated`
                  : "—"
              }
            />
            <Row label="engine db" value={health?.db?.split("/").pop() ?? "—"} />
            <Row label="api version" value={health?.version ?? "—"} />
            <Row label="write errors" value={health?.write_errors?.toString() ?? "—"} />
          </dl>
          {health?.detail && (
            <p className="mt-3 text-xs text-amber-300">{health.detail}</p>
          )}
        </section>

        <section className="rounded-lg border border-neutral-800 bg-neutral-900/40 p-4">
          <h2 className="mb-3 text-xs font-semibold uppercase tracking-widest text-neutral-500">
            Levels at this poll
          </h2>
          {poll ? (
            <dl className="grid grid-cols-2 gap-x-8 gap-y-2 text-sm sm:grid-cols-3">
              <Row label="spot" value={price(poll.spot)} />
              <Row label="flip" value={poll.flip === null ? "none in window" : price(poll.flip)} />
              <Row label="put wall" value={price(poll.put_wall)} />
              <Row label="call wall" value={price(poll.call_wall)} />
              <Row label="max accel" value={price(poll.max_accel)} />
              <Row label="max magnet" value={price(poll.max_magnet)} />
              <Row label="net gex" value={money(poll.net_gex)} />
              <Row label="poll ms" value={poll.poll_ms?.toString() ?? "—"} />
              <Row label="expiries" value={poll.expiries || "—"} />
            </dl>
          ) : (
            <p className="text-sm text-neutral-500">No poll recorded yet.</p>
          )}
          <p className="mt-3 text-xs text-neutral-600">
            Levels are named by gamma sign, not convention — labels arrive in
            step 2 from the API, never reimplemented here.
          </p>
        </section>

        <section className="rounded-lg border border-neutral-800 bg-neutral-900/40 p-4">
          <h2 className="mb-3 text-xs font-semibold uppercase tracking-widest text-neutral-500">
            Alerts this session ({alerts.length})
          </h2>
          {alerts.length === 0 ? (
            <p className="text-sm text-neutral-500">
              None since this page connected.
            </p>
          ) : (
            <ul className="space-y-1 text-sm">
              {alerts.slice(0, 8).map((a) => (
                <li key={a.alert_id} className="flex gap-3">
                  <span className="text-neutral-500">
                    {clockUtcToEt(parseUtc(a.ts))}
                  </span>
                  <span className="text-neutral-500">{a.kind}</span>
                  <span className="text-neutral-200">{a.title}</span>
                </li>
              ))}
            </ul>
          )}
        </section>
      </main>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-neutral-800/60 pb-1">
      <dt className="text-neutral-500">{label}</dt>
      <dd className="text-neutral-200">{value}</dd>
    </div>
  );
}
