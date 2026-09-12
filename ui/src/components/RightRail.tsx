import type { LatestPoll, Level, Strike } from "../api/types";
import { money, price } from "./format";

const LEVEL_NAME: Record<Level["kind"], string> = {
  flip: "flip",
  put_wall: "put wall",
  call_wall: "call wall",
  max_accel: "max accel",
  max_magnet: "max magnet",
  first_pos_above: "first +\u03b3",
};

const LABEL_TONE: Record<Level["label"], string> = {
  SUPPORT: "text-sky-400",
  RESISTANCE: "text-sky-400",
  TRAPDOOR: "text-rose-400",
  LAUNCHPAD: "text-rose-400",
};

/** Spec §2.3. Three stacked cards, all reading engine-computed values. */
export function RightRail({ poll }: { poll: LatestPoll }) {
  return (
    <div className="space-y-4">
      <NearestStructure strikes={poll.strikes} spot={poll.spot} levels={poll.context.levels} />
      <RegimeCard poll={poll} />
      <ShadowPosition poll={poll} />
    </div>
  );
}

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-lg border border-neutral-800 bg-neutral-900/40">
      <h2 className="border-b border-neutral-800 px-3 py-2 text-xs font-semibold uppercase tracking-widest text-neutral-500">
        {title}
      </h2>
      <div className="px-3 py-2">{children}</div>
    </section>
  );
}

/**
 * Ranked by |gex|, not by distance: the question is "what is near me that
 * matters", and a large wall two hundred points away shapes the session more
 * than a rounding-error strike one point away.
 */
function NearestStructure({
  strikes,
  spot,
  levels,
}: {
  strikes: Strike[];
  spot: number;
  levels: Level[];
}) {
  const kindFor = new Map<number, string>();
  for (const l of levels) {
    kindFor.set(l.strike, [kindFor.get(l.strike), LEVEL_NAME[l.kind]]
      .filter(Boolean).join(" · "));
  }
  const top = [...strikes]
    .filter((s) => s.gex !== 0)
    .sort((a, b) => Math.abs(b.gex) - Math.abs(a.gex))
    .slice(0, 5);

  return (
    <Card title="Nearest structure">
      {top.length === 0 ? (
        <p className="text-sm text-neutral-500">No strike gamma in window.</p>
      ) : (
        <ul className="space-y-1.5">
          {top.map((s) => {
            const kind = kindFor.get(s.strike);
            const d = s.strike - spot;
            return (
              <li
                key={s.strike}
                className="border-b border-neutral-800/50 pb-1.5 last:border-0 last:pb-0"
              >
                <div className="flex items-baseline justify-between gap-2">
                  <span className="font-medium text-neutral-200">
                    {price(s.strike)}
                  </span>
                  <span className="tabular-nums text-neutral-300">
                    {money(s.gex)}
                  </span>
                </div>
                <div className="flex items-baseline justify-between gap-2 text-xs">
                  <span className={LABEL_TONE[s.label]}>
                    {s.label}
                    {kind && <span className="text-neutral-600"> · {kind}</span>}
                  </span>
                  <span className="whitespace-nowrap text-neutral-500">
                    {d >= 0 ? "+" : ""}
                    {d.toFixed(0)} pts
                  </span>
                </div>
              </li>
            );
          })}
        </ul>
      )}
      <p className="pt-2 text-xs text-neutral-600">
        Ranked by |gamma|. Labels follow the gamma sign, from the engine.
      </p>
    </Card>
  );
}

function RegimeCard({ poll }: { poll: LatestPoll }) {
  const r = poll.context.regime;
  const tone = !r.called
    ? "text-neutral-400"
    : r.state === "POSITIVE"
      ? "text-sky-400"
      : "text-rose-400";
  const heading = !r.called
    ? "neutral (dead zone)"
    : r.state === "POSITIVE"
      ? "positive gamma"
      : "negative gamma";

  return (
    <Card title="Regime">
      <p className={`text-base font-semibold ${tone}`}>{heading}</p>
      <p className="mt-1 text-sm leading-snug text-neutral-400">{r.description}</p>
      <dl className="mt-2 space-y-1 text-xs text-neutral-600">
        <div className="flex justify-between">
          <dt>net gamma</dt>
          <dd className="text-neutral-400">{money(poll.net_gex)}</dd>
        </div>
        <div className="flex justify-between">
          <dt>session peak |net|</dt>
          <dd className="text-neutral-400">{money(r.session_peak_abs_net)}</dd>
        </div>
      </dl>
      <p className="pt-2 text-xs text-neutral-600">
        Dead zone is the engine&rsquo;s own threshold — the same one that
        decides whether an alert fires.
      </p>
    </Card>
  );
}

function ShadowPosition({ poll }: { poll: LatestPoll }) {
  const p = poll.context.position;
  return (
    <Card title="Shadow position">
      {p ? (
        <>
          <p className="text-base font-semibold text-neutral-100">
            {p.contracts}× {price(p.strike)}
            {p.direction > 0 ? "C" : "P"}
          </p>
          <dl className="mt-2 space-y-1 text-xs">
            <Row k="expiry" v={String(p.expiry)} />
            <Row k="entry premium" v={`$${p.entry_premium.toFixed(2)}`} />
            <Row k="entry spot" v={price(p.entry_spot)} />
            <Row k="spot now" v={price(poll.spot)} />
            <Row k="trigger level" v={price(p.level)} />
          </dl>
          <p className="mt-2 text-xs leading-snug text-neutral-500">{p.trigger}</p>
        </>
      ) : (
        <p className="text-sm leading-snug text-neutral-400">
          {poll.context.position_text.replace(/^📄\s*/, "")}
        </p>
      )}
      <p className="pt-2 text-xs text-neutral-600">
        Narration only — the strategy failed validation. Same text the Discord
        footer produces.
      </p>
    </Card>
  );
}

function Row({ k, v }: { k: string; v: string }) {
  return (
    <div className="flex justify-between gap-3">
      <dt className="text-neutral-600">{k}</dt>
      <dd className="text-neutral-300">{v}</dd>
    </div>
  );
}
