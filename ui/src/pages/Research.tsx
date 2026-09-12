import ReactECharts from "echarts-for-react";
import { useEffect, useMemo, useState } from "react";

import { api, isAbort } from "../api/client";
import type { Bundle, BundleSummary } from "../api/types";
import { price } from "../components/format";

/** Screen 3 — Research (spec §4). Read-only over the bundles on disk. */
export default function Research() {
  const [list, setList] = useState<BundleSummary[] | null>(null);
  const [name, setName] = useState<string | null>(null);
  const [bundle, setBundle] = useState<Bundle | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const ac = new AbortController();
    api.bundles(ac.signal)
      .then((b) => { setList(b); if (b.length && !name) setName(b[0].name); })
      .catch((e) => { if (!isAbort(e)) setError(String(e)); });
    return () => ac.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!name) return;
    const ac = new AbortController();
    setBundle(null);
    api.bundle(name, ac.signal)
      .then(setBundle)
      .catch((e) => { if (!isAbort(e)) setError(String(e)); });
    return () => ac.abort();
  }, [name]);

  return (
    <div className="space-y-4 p-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-sm font-semibold uppercase tracking-widest text-neutral-400">
          Research
        </h1>
        <select
          value={name ?? ""}
          onChange={(e) => setName(e.target.value)}
          className="rounded border border-neutral-700 bg-neutral-900 px-2 py-1 text-sm text-neutral-200"
        >
          {(list ?? []).map((b) => (
            <option key={b.name} value={b.name}>
              {b.name}
              {b.go_live_eligible ? "  ✓ all gates" : ""}
            </option>
          ))}
        </select>
        <span className="text-xs text-neutral-600">
          {list ? `${list.length} bundles on disk` : "…"}
        </span>
      </div>

      {error && (
        <p className="rounded border border-rose-900 bg-rose-950/40 p-3 text-sm text-rose-200">
          {error}
        </p>
      )}
      {name && !bundle && !error && (
        <p className="p-6 text-sm text-neutral-500">Loading {name}…</p>
      )}
      {bundle && <BundleView b={bundle} />}
    </div>
  );
}

function BundleView({ b }: { b: Bundle }) {
  const trades = b.trades as Record<string, number>[];
  const days = b.days as Record<string, number>[];

  const stats = useMemo(() => {
    const pnl = trades.map((t) => Number(t.pnl ?? 0));
    const wins = pnl.filter((p) => p > 0);
    const losses = pnl.filter((p) => p < 0);
    const gross = wins.reduce((a, c) => a + c, 0);
    const grossLoss = -losses.reduce((a, c) => a + c, 0);
    return {
      trades: pnl.length,
      net: pnl.reduce((a, c) => a + c, 0),
      winRate: pnl.length ? wins.length / pnl.length : 0,
      pf: grossLoss === 0 ? Infinity : gross / grossLoss,
      avgWin: wins.length ? gross / wins.length : 0,
      avgLoss: losses.length ? grossLoss / losses.length : 0,
    };
  }, [trades]);

  const equity = useMemo(() => {
    let running = 0;
    return days.map((d) => {
      running += Number(d.pnl ?? 0);
      return running;
    });
  }, [days]);

  const byReason = useMemo(() => {
    const m = new Map<string, { n: number; pnl: number }>();
    for (const t of trades) {
      const k = String(t.exit_reason ?? "—").replace("ExitReason.", "");
      const e = m.get(k) ?? { n: 0, pnl: 0 };
      m.set(k, { n: e.n + 1, pnl: e.pnl + Number(t.pnl ?? 0) });
    }
    return [...m.entries()].sort((a, c) => c[1].pnl - a[1].pnl);
  }, [trades]);

  const prov = b.mark_provenance;

  return (
    <div className="space-y-4">
      <section className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <Stat label="trades" value={String(stats.trades)} />
        <Stat label="net P&L" value={`$${Math.round(stats.net).toLocaleString()}`}
              tone={stats.net >= 0 ? "pos" : "neg"} />
        <Stat label="win rate" value={`${(stats.winRate * 100).toFixed(0)}%`} />
        <Stat label="profit factor"
              value={Number.isFinite(stats.pf) ? stats.pf.toFixed(2) : "∞"} />
        <Stat label="avg win" value={`$${Math.round(stats.avgWin).toLocaleString()}`} />
        <Stat label="avg loss" value={`$${Math.round(stats.avgLoss).toLocaleString()}`} />
      </section>

      <section className="rounded-lg border border-neutral-800 bg-neutral-900/40 p-3">
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-widest text-neutral-500">
          Equity curve & daily P&L
        </h2>
        {days.length === 0 ? (
          <p className="text-sm text-neutral-500">No day rows in this bundle.</p>
        ) : (
          <ReactECharts
            theme="dark"
            style={{ height: 300 }}
            notMerge
            option={{
              backgroundColor: "transparent",
              animation: false,
              grid: { left: 64, right: 56, top: 20, bottom: 40 },
              tooltip: { trigger: "axis", backgroundColor: "#111",
                         borderColor: "#333", textStyle: { color: "#e5e5e5", fontSize: 11 } },
              xAxis: { type: "category", data: days.map((d) => String(d.day)),
                       axisLabel: { color: "#666", fontSize: 9 },
                       axisLine: { lineStyle: { color: "#2a2a2a" } } },
              yAxis: [
                { type: "value", axisLabel: { color: "#666", fontSize: 9 },
                  splitLine: { lineStyle: { color: "#1f1f1f" } } },
                { type: "value", position: "right",
                  axisLabel: { color: "#666", fontSize: 9 }, splitLine: { show: false } },
              ],
              series: [
                { name: "daily P&L", type: "bar", yAxisIndex: 1,
                  data: days.map((d) => ({
                    value: Number(d.pnl ?? 0),
                    itemStyle: { color: Number(d.pnl ?? 0) >= 0 ? "#2f6f5f" : "#8f3f3f" },
                  })) },
                { name: "equity", type: "line", data: equity, showSymbol: false,
                  lineStyle: { color: "#e5e5e5", width: 1.5 } },
              ],
            }}
          />
        )}
      </section>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <section className="rounded-lg border border-neutral-800 bg-neutral-900/40 p-3">
          <h2 className="mb-2 text-xs font-semibold uppercase tracking-widest text-neutral-500">
            §10 Gates
          </h2>
          {b.gate_params && (
            <p className="mb-2 text-xs text-neutral-500">
              Judged at OOS maxDD ≤{" "}
              <span className="text-neutral-300">
                {((b.gate_params.max_drawdown_frac ?? 0) * 100).toFixed(1)}%
              </span>
              , OOS PF ≥{" "}
              <span className="text-neutral-300">
                {b.gate_params.min_profit_factor_oos}
              </span>
              , IS/OOS ≥ {b.gate_params.min_trades_is}/{b.gate_params.min_trades_oos}
            </p>
          )}
          {!b.gates ? (
            <p className="text-sm text-neutral-500">No gates.json in this bundle.</p>
          ) : (
            <ul className="space-y-1 text-sm">
              {Object.entries(b.gates).map(([k, g]) => (
                <li key={k} className="flex items-baseline gap-2">
                  <span className={g.passed ? "text-emerald-400" : "text-rose-400"}>
                    {g.passed ? "PASS" : "FAIL"}
                  </span>
                  <span className="text-neutral-300">{k}</span>
                  <span className="ml-auto text-xs text-neutral-500">{g.detail}</span>
                </li>
              ))}
            </ul>
          )}
          <div className="mt-3 border-t border-neutral-800 pt-2 text-xs">
            <span className="text-neutral-500">Mark provenance: </span>
            {prov ? (
              <span
                className={
                  (prov.fallback_pct ?? 0) < 5
                    ? "text-emerald-400"
                    : (prov.fallback_pct ?? 0) < 25
                      ? "text-amber-400"
                      : "text-rose-400"
                }
              >
                file {prov.file.toLocaleString()} · REST {prov.rest.toLocaleString()} ·
                model-fallback {prov.model_fallback.toLocaleString()} (
                {(prov.fallback_pct ?? 0).toFixed(1)}%)
              </span>
            ) : (
              <span className="text-neutral-600">
                not recorded — this bundle predates provenance being stored. Not
                the same as 0% fallback.
              </span>
            )}
          </div>
        </section>

        <section className="rounded-lg border border-neutral-800 bg-neutral-900/40 p-3">
          <h2 className="mb-2 text-xs font-semibold uppercase tracking-widest text-neutral-500">
            P&L by exit reason
          </h2>
          {byReason.length === 0 ? (
            <p className="text-sm text-neutral-500">No trades.</p>
          ) : (
            <table className="w-full text-sm">
              <tbody>
                {byReason.map(([k, v]) => (
                  <tr key={k} className="border-b border-neutral-800/50 last:border-0">
                    <td className="py-1 text-neutral-300">{k}</td>
                    <td className="py-1 text-right text-xs text-neutral-500">{v.n}</td>
                    <td className={`py-1 text-right tabular-nums ${v.pnl >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                      ${Math.round(v.pnl).toLocaleString()}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      </div>

      <section className="rounded-lg border border-neutral-800 bg-neutral-900/40 p-3">
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-widest text-neutral-500">
          Trade log ({trades.length})
        </h2>
        <div className="max-h-80 overflow-auto">
          <table className="w-full text-xs">
            <thead className="sticky top-0 bg-neutral-900 text-neutral-500">
              <tr>
                {["day", "kind", "dir", "qty", "entry", "exit", "reason", "P&L"].map((h) => (
                  <th key={h} className="px-2 py-1 text-left font-normal">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {trades.slice(0, 400).map((t, i) => (
                <tr key={i} className="border-b border-neutral-800/40">
                  <td className="px-2 py-1 text-neutral-400">{String(t.day ?? "")}</td>
                  <td className="px-2 py-1 text-neutral-400">{String(t.kind ?? "")}</td>
                  <td className="px-2 py-1">{Number(t.direction) > 0 ? "C" : "P"}</td>
                  <td className="px-2 py-1">{String(t.contracts ?? "")}</td>
                  <td className="px-2 py-1">{price(Number(t.entry_fill), 2)}</td>
                  <td className="px-2 py-1">{price(Number(t.exit_fill), 2)}</td>
                  <td className="px-2 py-1 text-neutral-500">
                    {String(t.exit_reason ?? "").replace("ExitReason.", "")}
                  </td>
                  <td className={`px-2 py-1 text-right tabular-nums ${Number(t.pnl) >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                    {Math.round(Number(t.pnl)).toLocaleString()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {trades.length > 400 && (
          <p className="pt-2 text-xs text-neutral-600">
            Showing the first 400 of {trades.length}.
          </p>
        )}
      </section>

      {b.summary_md && (
        <details className="rounded-lg border border-neutral-800 bg-neutral-900/40 p-3">
          <summary className="cursor-pointer text-xs font-semibold uppercase tracking-widest text-neutral-500">
            summary.md
          </summary>
          <pre className="mt-2 overflow-auto whitespace-pre-wrap text-xs text-neutral-400">
            {b.summary_md}
          </pre>
        </details>
      )}

      <p className="text-xs text-neutral-600">
        Read-only: bundles are read from disk and never re-run. Putting a
        strategy evaluation behind a URL is the casual re-running the
        pre-registration exists to prevent.
      </p>
    </div>
  );
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: "pos" | "neg" }) {
  return (
    <div className="rounded-lg border border-neutral-800 bg-neutral-900/40 px-3 py-2">
      <div className="text-xs text-neutral-500">{label}</div>
      <div className={`text-lg font-semibold ${
        tone === "pos" ? "text-emerald-400" : tone === "neg" ? "text-rose-400" : "text-neutral-100"
      }`}>{value}</div>
    </div>
  );
}
