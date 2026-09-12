import { useEffect, useMemo, useState } from "react";

import { api, isAbort, parseUtc } from "../api/client";
import type { TapePrint, TapeStats } from "../api/types";
import { money, price } from "./format";

/**
 * Spec §15 — the classified tape. Prints in sequence, unaggregated.
 *
 * §15.5 is a hard boundary and this component is where it would be violated
 * first. The panel shows prints. It does not label a sequence "absorption",
 * "aggression", a "sweep", or "institutional"; it carries no buy-pressure
 * gauge; it makes no sound and nothing flashes. Those are readings, and this
 * project's own control tests found its flow-derived entry logic did not beat
 * random out of sample — so there is no basis for asserting any of them, and a
 * tape that demanded attention would compete with the alerts, which earned it.
 *
 * The header is the reason this is also a diagnostic: unclassified % and
 * gamma-lookup misses are shown permanently, not behind a menu. A classifier
 * returning 0 for everything, or a gamma lookup that misses every contract,
 * produces a flat overlay that looks exactly like a quiet market. That is not
 * hypothetical — the gamma key-shape mismatch found while wiring the overlay
 * would have shown here as every print reading `γ 0`.
 */
export function TapePanel({ underlying }: { underlying: string }) {
  const [open, setOpen] = useState(false);      // collapsed by default (§15.4)
  const [prints, setPrints] = useState<TapePrint[]>([]);
  const [stats, setStats] = useState<TapeStats | null>(null);
  const [minSize, setMinSize] = useState("");
  const [strike, setStrike] = useState("");
  const [right, setRight] = useState("");
  const [side, setSide] = useState("");

  useEffect(() => {
    if (!open) return;
    const ac = new AbortController();
    const load = () =>
      api
        .tape(underlying, {
          limit: 200,
          min_size: minSize ? Number(minSize) : undefined,
          strike: strike ? Number(strike) : undefined,
          right: right || undefined,
          side: side === "" ? undefined : Number(side),
        }, ac.signal)
        .then((d) => { setPrints(d.prints); setStats(d.stats); })
        .catch((e) => { if (!isAbort(e)) setPrints([]); });
    void load();
    const id = window.setInterval(load, 2000);
    return () => { ac.abort(); window.clearInterval(id); };
  }, [open, underlying, minSize, strike, right, side]);

  const blockThreshold = stats?.block_threshold ?? null;
  const degraded = useMemo(() => {
    if (!stats || !stats.prints_seen) return false;
    return (stats.unclassified_pct ?? 0) > 0.25 || (stats.gamma_miss_pct ?? 0) > 0.25;
  }, [stats]);

  return (
    <section className="rounded-lg border border-neutral-800 bg-neutral-900/40">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full flex-wrap items-center gap-x-4 gap-y-1 px-4 py-2 text-left"
      >
        <span className="text-xs font-semibold uppercase tracking-widest text-neutral-500">
          {open ? "▾" : "▸"} Classified tape
        </span>
        <Header stats={stats} degraded={degraded} />
      </button>

      {open && (
        <>
          <div className="flex flex-wrap items-center gap-2 border-t border-neutral-800 px-4 py-2 text-xs">
            <Filter label="min size" value={minSize} onChange={setMinSize}
                    placeholder="any" width="w-20" />
            <Filter label="strike" value={strike} onChange={setStrike}
                    placeholder="any" width="w-24" />
            <Select label="right" value={right} onChange={setRight}
                    options={[["", "both"], ["C", "calls"], ["P", "puts"]]} />
            <Select label="side" value={side} onChange={setSide}
                    options={[["", "all"], ["1", "buy"], ["-1", "sell"],
                              ["0", "unclassified"]]} />
            {blockThreshold !== null && (
              <span className="ml-auto text-neutral-600">
                rows ≥ {blockThreshold.toLocaleString()} contracts emphasised
              </span>
            )}
          </div>

          <div className="max-h-96 overflow-auto">
            {prints.length === 0 ? (
              <p className="px-4 py-6 text-center text-sm text-neutral-500">
                No prints. The engine publishes classified prints only while the
                feed is connected and the market is trading.
              </p>
            ) : (
              <table className="w-full text-xs tabular-nums">
                <thead className="sticky top-0 bg-neutral-900 text-neutral-500">
                  <tr>
                    {["time", "strike", "R", "size", "price", "premium", "side",
                      "γ used", "Δ dealer γ"].map((h) => (
                      <th key={h} className="px-2 py-1 text-left font-normal">
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {prints.map((p) => (
                    <Row key={p.seq} p={p} threshold={blockThreshold} />
                  ))}
                </tbody>
              </table>
            )}
          </div>

          <p className="border-t border-neutral-800 px-4 py-2 text-xs text-neutral-600">
            Prints in the order they arrived, unaggregated and unlabelled.
            <span className="text-neutral-500">
              {" "}
              γ used is the gamma the lookup returned — 0 means the print moved
              the map by nothing.
            </span>{" "}
            Held in a bounded ring; not a session archive.
          </p>
        </>
      )}
    </section>
  );
}

function Header({ stats, degraded }: { stats: TapeStats | null; degraded: boolean }) {
  if (!stats) return <span className="text-xs text-neutral-600">—</span>;
  const pct = (v: number | null) => (v === null ? "—" : `${(v * 100).toFixed(1)}%`);
  return (
    <span
      className={`flex flex-wrap items-center gap-x-4 text-xs ${
        degraded ? "text-amber-300" : "text-neutral-500"
      }`}
    >
      <span>{stats.prints_seen.toLocaleString()} prints</span>
      <span>{stats.contracts_seen.toLocaleString()} contracts</span>
      <span className={degraded ? "font-semibold" : ""}>
        unclassified {stats.unclassified.toLocaleString()} (
        {pct(stats.unclassified_pct)})
      </span>
      <span className={degraded ? "font-semibold" : ""}>
        γ-lookup misses {stats.gamma_misses.toLocaleString()} (
        {pct(stats.gamma_miss_pct)})
      </span>
      {degraded && (
        <span className="rounded bg-amber-500/20 px-2 py-0.5">
          overlay degraded — the map is thinner than it looks
        </span>
      )}
    </span>
  );
}

const SIDE = {
  1: { sym: "▲", label: "buy", tone: "text-sky-400" },
  "-1": { sym: "▼", label: "sell", tone: "text-rose-400" },
  0: { sym: "·", label: "unclassified", tone: "text-neutral-500" },
} as const;

function Row({ p, threshold }: { p: TapePrint; threshold: number | null }) {
  const s = SIDE[String(p.side) as "1" | "-1" | "0"] ?? SIDE[0];
  const block = threshold !== null && p.size >= threshold;
  const missed = p.side !== 0 && p.gamma_used === 0;
  return (
    <tr
      className={`border-b border-neutral-800/40 ${
        block ? "bg-neutral-800/50 font-semibold text-neutral-100" : ""
      }`}
    >
      <td className="px-2 py-0.5 text-neutral-500">
        {parseUtc(p.ts).toLocaleTimeString("en-US", {
          hour12: false, timeZone: "America/New_York",
        })}
      </td>
      <td className="px-2 py-0.5">{price(p.strike)}</td>
      <td className="px-2 py-0.5 text-neutral-400">{p.option_right}</td>
      <td className="px-2 py-0.5">{p.size.toLocaleString()}</td>
      <td className="px-2 py-0.5">{p.price.toFixed(2)}</td>
      <td className="px-2 py-0.5 text-neutral-400">
        ${Math.round(p.premium).toLocaleString()}
      </td>
      {/* symbol AND colour: the unclassified case must be visually distinct,
          not merely the absence of a colour (§15.4) */}
      <td className={`px-2 py-0.5 ${s.tone}`}>
        {s.sym} {s.label}
      </td>
      <td className={`px-2 py-0.5 ${missed ? "text-amber-400" : "text-neutral-500"}`}>
        {p.gamma_used === 0 ? "0" : p.gamma_used.toFixed(4)}
      </td>
      <td className="px-2 py-0.5 text-neutral-500">
        {p.dealer_gamma_delta === 0 ? "—" : money(p.dealer_gamma_delta)}
      </td>
    </tr>
  );
}

function Filter({ label, value, onChange, placeholder, width }: {
  label: string; value: string; onChange: (v: string) => void;
  placeholder: string; width: string;
}) {
  return (
    <label className="flex items-center gap-1 text-neutral-600">
      {label}
      <input
        value={value}
        inputMode="numeric"
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value.replace(/[^\d.]/g, ""))}
        className={`${width} rounded border border-neutral-700 bg-neutral-900 px-2 py-1 text-neutral-200`}
      />
    </label>
  );
}

function Select({ label, value, onChange, options }: {
  label: string; value: string; onChange: (v: string) => void;
  options: readonly (readonly [string, string])[];
}) {
  return (
    <label className="flex items-center gap-1 text-neutral-600">
      {label}
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="rounded border border-neutral-700 bg-neutral-900 px-2 py-1 text-neutral-200"
      >
        {options.map(([v, l]) => (
          <option key={v} value={v}>{l}</option>
        ))}
      </select>
    </label>
  );
}
