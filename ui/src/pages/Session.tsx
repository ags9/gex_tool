import { StableChart } from "../components/StableChart";
import { useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";

import { api, isAbort } from "../api/client";
import {
  INSTRUMENT_LABEL,
  INSTRUMENTS,
  strikeDivisor,
  useInstrument,
  useUnits,
} from "../api/prefs";
import type { Alert, Heatmap, Poll, Premium, ShadowTrade } from "../api/types";
import { money, price } from "../components/format";
import { hhmm, todayEt } from "../components/minute";

interface Data {
  polls: Poll[];
  premium: Premium[];
  heat: Heatmap;
  alerts: Alert[];
  trades: ShadowTrade[];
}

const C = {
  net: "#e5e5e5",
  oi: "#5fb3e8",
  flow: "#e8c95f",
  spot: "#e5e5e5",
  flip: "#e8c95f",
  putWall: "#e87b7b",
  callWall: "#5fb3e8",
  callBought: "#5fb3e8",
  callSold: "#2f6f9f",
  putBought: "#e87b7b",
  putSold: "#9f3f3f",
  grid: "#1f1f1f",
  axis: "#666",
};

/** Screen 2 — Session (spec §3). Four panels, one shared brushable x-axis. */
export default function Session() {
  const { date } = useParams();
  const day = date ?? todayEt();
  const [instrument, setInstrument] = useInstrument();
  const [units] = useUnits();
  const divisor = strikeDivisor(units, instrument);
  const [data, setData] = useState<Data | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const ac = new AbortController();
    setData(null);
    setError(null);
    void (async () => {
      try {
        const [polls, premium, heat, alerts, trades] = await Promise.all([
          api.polls(day, instrument, ac.signal),
          api.premium(day, ac.signal),
          api.heatmap(day, instrument, ac.signal),
          api.alerts(day, instrument, ac.signal),
          api.trades(day, instrument, ac.signal),
        ]);
        if (!ac.signal.aborted) setData({ polls, premium, heat, alerts, trades });
      } catch (e) {
        if (!ac.signal.aborted && !isAbort(e)) setError(String(e));
      }
    })();
    return () => ac.abort();
  }, [day, instrument]);

  const option = useMemo(
    () => (data ? buildOption(data, divisor) : null),
    [data, divisor],
  );

  const asOf = data?.polls.length
    ? hhmm(data.polls[data.polls.length - 1].minute_of_day)
    : null;
  const unclassified = data?.premium.length
    ? data.premium[data.premium.length - 1].unclassified
    : 0;
  const counted = data?.premium.length
    ? data.premium[data.premium.length - 1].trades_counted
    : 0;

  return (
    <div className="space-y-4 p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <h1 className="text-sm font-semibold uppercase tracking-widest text-neutral-400">
          Session {day}
        </h1>
        <span className="flex items-center gap-1 text-xs">
          {INSTRUMENTS.map((k) => (
            <button
              key={k}
              type="button"
              onClick={() => setInstrument(k)}
              className={`rounded px-2 py-1 transition-colors ${
                instrument === k
                  ? "bg-neutral-700 text-neutral-100"
                  : "text-neutral-500 hover:bg-neutral-800"
              }`}
            >
              {INSTRUMENT_LABEL[k]}
            </button>
          ))}
          <span className="ml-3 text-neutral-500">
            {data ? `${data.polls.length} polls` : "…"}
            {asOf && ` · last ${asOf} ET`}
            {divisor !== 1 && " · strikes in SPY"}
          </span>
        </span>
      </div>

      {error && (
        <p className="rounded border border-rose-900 bg-rose-950/40 p-3 text-sm text-rose-200">
          {error}
        </p>
      )}

      {data && data.polls.length === 0 && (
        <p className="rounded border border-neutral-800 bg-neutral-900/40 p-6 text-center text-sm text-neutral-500">
          No {INSTRUMENT_LABEL[instrument]} polls stored for {day}.
        </p>
      )}

      {option && data && data.polls.length > 0 && (
        <section className="rounded-lg border border-neutral-800 bg-neutral-900/40 p-2">
          <StableChart
            option={option}
            style={{ height: 1180 }}
            notMerge
          />
        </section>
      )}

      {data && (
        <div className="space-y-1 rounded-lg border border-neutral-800 bg-neutral-900/40 p-4 text-xs text-neutral-600">
          <p>
            <span className="text-neutral-400">Classification.</span> Flow and
            premium are signed by the tick rule, roughly 75–80% accurate versus
            NBBO in the literature.{" "}
            <span className="text-neutral-400">
              The error on our own data is unquantified
            </span>{" "}
            — the NBBO-vs-tick-rule study on the two full-quote days has not
            been run.
          </p>
          <p>
            <span className="text-neutral-400">Unclassified.</span>{" "}
            {unclassified.toLocaleString()} prints took no side (zero ticks with
            no prior direction) against {counted.toLocaleString()} counted
            {counted + unclassified > 0 &&
              ` — ${((unclassified / (counted + unclassified)) * 100).toFixed(1)}% excluded`}
            . They are in neither premium series.
          </p>
          <p>
            <span className="text-neutral-400">Gaps are gaps.</span> Where the
            flow overlay was off, OI and flow series break rather than dropping
            to zero. Crossover dots mark factual events — no consequence is
            implied.
          </p>
        </div>
      )}
    </div>
  );
}

function buildOption(d: Data, divisor: number) {
  // §10.1 never mixes: the price panel and the heatmap rows are the only
  // strike-bearing axes here, and both take the same divisor.
  const sc = (v: number | null) => (v === null || v === undefined ? null : v / divisor);
  const minutes = d.polls.map((p) => p.minute_of_day);
  const cat = minutes.map(hhmm);
  const idx = new Map(minutes.map((m, i) => [m, i]));
  const at = (m: number) => idx.get(m) ?? null;

  const series = (f: (p: Poll) => number | null) =>
    d.polls.map((p) => {
      const v = f(p);
      return v === null || v === undefined ? null : v;
    });

  // Event markers on every panel: what the bot did, against the map that
  // produced it (spec §3.5).
  const events = [
    ...d.alerts
      .map((a) => {
        const m = d.polls.find((p) => p.poll_id === a.poll_id)?.minute_of_day;
        return m === undefined ? null : { m, label: a.kind, colour: "#e8c95f" };
      })
      .filter(Boolean as unknown as (x: unknown) => x is { m: number; label: string; colour: string }),
    ...d.trades.flatMap((t) => [
      { m: t.entry_minute, label: "entry", colour: "#5fb3e8" },
      ...(t.exit_minute !== null
        ? [{ m: t.exit_minute, label: "exit", colour: "#e87b7b" }]
        : []),
    ]),
  ];

  const markLineFor = (showLabel: boolean) => ({
    silent: true,
    symbol: "none" as const,
    data: events
      .map((e) => {
        const i = at(e.m);
        return i === null
          ? null
          : {
              xAxis: i,
              lineStyle: { color: e.colour, width: 1, type: "dotted" as const, opacity: 0.55 },
              label: showLabel
                ? {
                    formatter: e.label,
                    color: e.colour,
                    fontSize: 9,
                    rotate: 90,
                    position: "insideEndTop" as const,
                  }
                : { show: false },
            };
      })
      .filter(Boolean),
  });

  // premium panel: index by minute so it aligns with the shared axis
  const premAt = new Map(d.premium.map((p) => [p.minute_of_day, p]));
  const prem = (f: (p: Premium) => number) =>
    minutes.map((m) => {
      const p = premAt.get(m);
      return p ? f(p) : null;
    });

  // Crossovers: net call premium overtaking net put premium, or the reverse.
  // Factual events, marked without implied consequence (spec §3.3).
  const netCall = prem((p) => p.call_bought - p.call_sold);
  const netPut = prem((p) => p.put_bought - p.put_sold);
  const crossings: { xAxis: number; label: string }[] = [];
  for (let i = 1; i < minutes.length; i += 1) {
    const a = netCall[i - 1], b = netPut[i - 1], c = netCall[i], e = netPut[i];
    if (a === null || b === null || c === null || e === null) continue;
    if (Math.sign(a - b) !== 0 && Math.sign(a - b) !== Math.sign(c - e)) {
      crossings.push({ xAxis: i, label: hhmm(minutes[i]) });
    }
  }

  const heatCells = d.heat.cells
    .map(([m, k, g]) => {
      const x = at(m);
      const y = d.heat.strikes.indexOf(k);
      return x === null || y < 0 ? null : [x, y, g];
    })
    .filter(Boolean) as number[][];
  const maxAbs = Math.max(1, ...heatCells.map((c) => Math.abs(c[2])));

  const xAxisBase = {
    type: "category" as const,
    data: cat,
    axisLine: { lineStyle: { color: "#2a2a2a" } },
    axisLabel: { color: C.axis, fontSize: 9 },
    splitLine: { show: false },
  };
  const yAxisBase = {
    type: "value" as const,
    axisLabel: { color: C.axis, fontSize: 9 },
    splitLine: { lineStyle: { color: C.grid } },
  };
  const line = (
    name: string, data: (number | null)[], colour: string,
    xi: number, yi: number, extra: Record<string, unknown> = {},
  ) => ({
    name, type: "line" as const, data,
    xAxisIndex: xi, yAxisIndex: yi,
    showSymbol: false, connectNulls: false,   // §6.3: NULL is a gap, not zero
    lineStyle: { color: colour, width: 1.5 },
    itemStyle: { color: colour },
    ...extra,
  });

  const seriesList = [
    // 3.1 net gamma over time
    line("net gamma", series((p) => p.net_gex), C.net, 0, 0,
         { markLine: markLineFor(true) }),
    line("OI net", series((p) => p.oi_net), C.oi, 0, 0),
    line("flow net", series((p) => p.flow_net), C.flow, 0, 0),
    // 3.2 level drift vs spot
    line("spot", series((p) => sc(p.spot)), C.spot, 1, 1,
         { lineStyle: { color: C.spot, width: 2 }, markLine: markLineFor(false) }),
    line("flip", series((p) => sc(p.flip)), C.flip, 1, 1,
         { lineStyle: { color: C.flip, width: 1, type: "dashed" } }),
    line("put wall", series((p) => sc(p.put_wall)), C.putWall, 1, 1,
         { step: "end" as const }),
    line("call wall", series((p) => sc(p.call_wall)), C.callWall, 1, 1,
         { step: "end" as const }),
    // 3.3 premium drift by side — four series, never two
    line("calls bought", prem((p) => p.call_bought), C.callBought, 2, 2,
         { markLine: markLineFor(false),
           markPoint: { symbol: "circle", symbolSize: 7, data: crossings.map((c) => ({
             xAxis: c.xAxis, yAxis: netCall[c.xAxis] ?? 0,
             itemStyle: { color: "#e8c95f" },
             label: { formatter: c.label, color: "#e8c95f", fontSize: 9,
                      position: "top" as const } })) } }),
    line("calls sold", prem((p) => p.call_sold), C.callSold, 2, 2),
    line("puts bought", prem((p) => p.put_bought), C.putBought, 2, 2),
    line("puts sold", prem((p) => p.put_sold), C.putSold, 2, 2),
    // 3.4 heatmap
    { name: "strike gamma", type: "heatmap" as const, xAxisIndex: 3, yAxisIndex: 4,
      data: heatCells, progressive: 4000, emphasis: { disabled: true } },
    // the one permitted dual axis, labelled as context
    line("spot (context)", series((p) => sc(p.spot)), "#8f8f9f", 2, 3,
         { lineStyle: { color: "#8f8f9f", width: 1, opacity: 0.7, type: "dotted" } }),
  ];

  // Derived, never hardcoded: a heatmap with no visualMap pointed at it draws
  // nothing at all, silently. An index literal drifts the moment a series is
  // inserted above it — which is exactly how this panel first shipped blank.
  const heatIndex = seriesList.findIndex((x) => x.type === "heatmap");

  return {
    backgroundColor: "transparent",
    animation: false,
    tooltip: { trigger: "axis" as const, backgroundColor: "#111",
               borderColor: "#333", textStyle: { color: "#e5e5e5", fontSize: 11 },
               axisPointer: { link: [{ xAxisIndex: "all" }] } },
    axisPointer: { link: [{ xAxisIndex: "all" }] },
    legend: [
      { top: 4, left: 60, textStyle: { color: C.axis, fontSize: 10 },
        data: ["net gamma", "OI net", "flow net"] },
      { top: 300, left: 60, textStyle: { color: C.axis, fontSize: 10 },
        data: ["spot", "flip", "put wall", "call wall"] },
      { top: 596, left: 60, textStyle: { color: C.axis, fontSize: 10 },
        data: ["calls bought", "calls sold", "puts bought", "puts sold", "spot (context)"] },
    ],
    grid: [
      { left: 64, right: 56, top: 30, height: 230 },
      { left: 64, right: 56, top: 326, height: 230 },
      { left: 64, right: 56, top: 622, height: 230 },
      { left: 64, right: 56, top: 918, height: 200 },
    ],
    xAxis: [
      { ...xAxisBase, gridIndex: 0, axisLabel: { show: false } },
      { ...xAxisBase, gridIndex: 1, axisLabel: { show: false } },
      { ...xAxisBase, gridIndex: 2, axisLabel: { show: false } },
      { ...xAxisBase, gridIndex: 3 },
    ],
    yAxis: [
      { ...yAxisBase, gridIndex: 0,         axisLabel: { ...yAxisBase.axisLabel, formatter: (v: number) => money(v) } },
      { ...yAxisBase, gridIndex: 1,         scale: true, axisLabel: { ...yAxisBase.axisLabel, formatter: (v: number) => price(v) } },
      { ...yAxisBase, gridIndex: 2,         axisLabel: { ...yAxisBase.axisLabel, formatter: (v: number) => money(v) } },
      { ...yAxisBase, gridIndex: 2, position: "right" as const, scale: true,
        splitLine: { show: false },
        axisLabel: { ...yAxisBase.axisLabel, formatter: (v: number) => price(v) } },
      { type: "category" as const, gridIndex: 3,
        data: d.heat.strikes.map((s) => price(s / divisor, divisor === 1 ? 0 : 2)),
        axisLabel: { color: C.axis, fontSize: 8, interval: Math.max(0, Math.floor(d.heat.strikes.length / 12)) },
        splitLine: { show: false } },
    ],
    visualMap: {
      min: -maxAbs, max: maxAbs, calculable: false, show: true,
      orient: "vertical" as const, right: 4, top: 930, itemHeight: 120,
      textStyle: { color: C.axis, fontSize: 9 },
      inRange: { color: ["#c0392b", "#5a2a2a", "#151515", "#24506e", "#3498db"] },
      seriesIndex: heatIndex,
    },
    dataZoom: [
      { type: "slider" as const, xAxisIndex: [0, 1, 2, 3], bottom: 6, height: 18,
        fillerColor: "#ffffff10", borderColor: "#2a2a2a",
        handleStyle: { color: "#555" }, textStyle: { color: C.axis, fontSize: 9 } },
      { type: "inside" as const, xAxisIndex: [0, 1, 2, 3] },
    ],
    series: seriesList,
  };
}
