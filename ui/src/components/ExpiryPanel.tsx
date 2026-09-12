import ReactECharts from "echarts-for-react";
import { useEffect, useState } from "react";

import { api, isAbort } from "../api/client";
import type { Expiry } from "../api/types";
import { money } from "./format";

/**
 * Spec §11.2. Gamma expiring on each date, plus the share still alive after
 * it. OPEX dates are marked from the calendar rule in `clock.opex_kind`, not
 * inferred from OI size — inferring it from size would make "this is OPEX"
 * and "OPEX is large" the same claim.
 *
 * States facts. "57% of current gamma expires 9/18", never "structure
 * deteriorates into Friday".
 */
export function ExpiryPanel({ pollId }: { pollId: number }) {
  const [rows, setRows] = useState<Expiry[] | null>(null);

  useEffect(() => {
    const ac = new AbortController();
    setRows(null);
    api.expiries(pollId, ac.signal)
      .then(setRows)
      .catch((e) => { if (!isAbort(e)) setRows([]); });
    return () => ac.abort();
  }, [pollId]);

  if (!rows || rows.length === 0) {
    return (
      <section className="rounded-lg border border-neutral-800 bg-neutral-900/40 p-4">
        <h2 className="text-xs font-semibold uppercase tracking-widest text-neutral-500">
          Gamma by expiry
        </h2>
        <p className="mt-2 text-sm text-neutral-500">
          {rows ? "No expiry profile stored for this poll." : "Loading…"}
        </p>
      </section>
    );
  }

  const total = rows.reduce((a, r) => a + r.gamma, 0);
  const biggest = rows.reduce((a, r) => (Math.abs(r.gamma) > Math.abs(a.gamma) ? r : a));

  const option = {
    backgroundColor: "transparent",
    animation: false,
    grid: { left: 62, right: 52, top: 24, bottom: 46 },
    tooltip: {
      trigger: "axis" as const, backgroundColor: "#111", borderColor: "#333",
      textStyle: { color: "#e5e5e5", fontSize: 11 },
      formatter: (p: any) => {
        const r = rows[p?.[0]?.dataIndex ?? 0];
        if (!r) return "";
        const line = (k: string, v: string) =>
          `<div style="display:flex;gap:10px;justify-content:space-between">` +
          `<span style="color:#888">${k}</span><span>${v}</span></div>`;
        return `<b>${r.expiry}</b>${r.opex ? ` <span style="color:#e8c95f">${r.opex} OPEX</span>` : ""}` +
          line("gamma", money(r.gamma)) +
          line("share", r.pct_of_total === null ? "—" : `${(r.pct_of_total * 100).toFixed(1)}%`) +
          line("delta", `${(r.delta / 1e9).toFixed(2)}B`) +
          line("open interest", r.oi.toLocaleString()) +
          line("put/call OI", r.put_call_oi === null ? "—" : r.put_call_oi.toFixed(2));
      },
    },
    xAxis: {
      type: "category" as const,
      data: rows.map((r) => r.expiry.slice(5)),
      axisLabel: { color: "#666", fontSize: 9, rotate: 45 },
      axisLine: { lineStyle: { color: "#2a2a2a" } },
    },
    yAxis: [
      { type: "value" as const, axisLabel: { color: "#666", fontSize: 9,
          formatter: (v: number) => money(v) },
        splitLine: { lineStyle: { color: "#1f1f1f" } } },
      { type: "value" as const, position: "right" as const, min: 0, max: 1,
        axisLabel: { color: "#666", fontSize: 9,
          formatter: (v: number) => `${(v * 100).toFixed(0)}%` },
        splitLine: { show: false } },
    ],
    series: [
      {
        name: "gamma expiring", type: "bar" as const,
        data: rows.map((r) => ({
          value: r.gamma,
          itemStyle: { color: r.opex ? "#e8c95f" : r.gamma >= 0 ? "#2f6f9f" : "#9f3f3f" },
        })),
      },
      {
        name: "still alive after", type: "line" as const, yAxisIndex: 1,
        data: rows.map((r) => r.pct_remaining_after),
        showSymbol: false, lineStyle: { color: "#8f8f9f", width: 1, type: "dashed" as const },
      },
    ],
  };

  return (
    <section className="rounded-lg border border-neutral-800 bg-neutral-900/40">
      <div className="flex flex-wrap items-baseline justify-between gap-2 border-b border-neutral-800 px-4 py-2">
        <h2 className="text-xs font-semibold uppercase tracking-widest text-neutral-500">
          Gamma by expiry
        </h2>
        <span className="text-xs text-neutral-500">
          {total !== 0 && (
            <>
              {(Math.abs(biggest.gamma / total) * 100).toFixed(0)}% of the next
              30 days&rsquo; gamma expires {biggest.expiry}
              {biggest.opex && (
                <span className="text-amber-300"> ({biggest.opex} OPEX)</span>
              )}
            </>
          )}
        </span>
      </div>
      <ReactECharts option={option} style={{ height: 220 }} notMerge theme="dark" />
      <p className="border-t border-neutral-800 px-4 py-2 text-xs text-neutral-600">
        Bars: dealer gamma expiring on each date. Dashed line: share of current
        gamma still alive after it. OPEX marked from the exchange calendar.
      </p>
    </section>
  );
}
