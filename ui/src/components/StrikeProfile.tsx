import { StableChart } from "./StableChart";
import { useMemo, useState } from "react";

import type { Level, Strike } from "../api/types";
import { money, price } from "./format";

export type Source = "flow" | "oi" | "volume" | "dex";

interface Props {
  strikes: Strike[];
  levels: Level[];
  spot: number;
  overlayOn: boolean;
  /** §10.1 display transform. Strikes are divided by this; gamma is not. */
  divisor: number;
  unitsLabel: string;
}

const OI_COLOUR_POS = "#2f6f9f";
const OI_COLOUR_NEG = "#9f3f3f";
const FLOW_COLOUR_POS = "#5fb3e8";
const FLOW_COLOUR_NEG = "#e87b7b";

const LEVEL_STYLE: Record<Level["kind"], { colour: string; text: string }> = {
  flip: { colour: "#e8c95f", text: "flip" },
  put_wall: { colour: "#8f8f9f", text: "put wall" },
  call_wall: { colour: "#8f8f9f", text: "call wall" },
  max_accel: { colour: "#e87b7b", text: "max accel" },
  max_magnet: { colour: "#5fb3e8", text: "max magnet" },
  first_pos_above: { colour: "#6f6f7f", text: "first +γ above" },
};

/**
 * Spec §2.2. Strikes descend the y axis, zero line centred.
 *
 * In flow mode each bar is stacked: the OI baseline and today's classified
 * flow in separate shades. That split is the thing no public GEX product
 * shows, and it is the whole reason the live feed exists — OI publishes once
 * pre-market and cannot see 0DTE.
 *
 * Levels are drawn from the API's annotations. The UI does not decide what a
 * level is called: `label` arrives already computed by levels.level_label, so
 * the chart and the Discord alert cannot disagree.
 */
export function StrikeProfile({ strikes, levels, spot, overlayOn, divisor,
                                unitsLabel }: Props) {
  const [source, setSource] = useState<Source>("flow");
  const hasVolume = strikes.some((s) => s.volume !== null && s.volume !== undefined);
  const hasDex = strikes.some((s) => s.dex !== null && s.dex !== undefined);
  // A flow component that is identically zero is not worth stacking — and
  // ECharts draws nothing at all for a stack whose second series is uniformly
  // zero, which is how this surfaced. Render the single series instead and
  // say so, rather than showing an empty chart that claims a split.
  const hasFlow = strikes.some((s) => (s.flow_gex ?? 0) !== 0);
  const effective: Source =
    source === "flow" && !overlayOn ? "oi"
    : source === "volume" && !hasVolume ? "oi"
    : source === "dex" && !hasDex ? "oi"
    : source;

  const rows = useMemo(
    () => [...strikes].sort((a, b) => b.strike - a.strike),
    [strikes],
  );

  const option = useMemo(() => {
    // §10.1 is a DISPLAY transform, so the divisor touches formatting only.
    // Categories, the spot index and the level indices all stay in raw
    // strikes: scaling the values the axis is positioned by mixes a
    // presentation concern into the geometry, and the bars stopped drawing
    // when it did. `fmt` is the single place the transform is applied.
    const fmt = (v: number) => price(v / divisor, divisor === 1 ? 0 : 2);
    const categories = rows.map((r) => r.strike);

    // Position of an arbitrary price on a category axis: interpolate between
    // the two strikes that bracket it, so the spot line lands where the price
    // actually is rather than snapping to the nearest listed strike.
    const indexOf = (value: number): number => {
      if (categories.length === 0) return 0;
      if (value >= categories[0]) return 0;
      if (value <= categories[categories.length - 1]) return categories.length - 1;
      for (let i = 0; i < categories.length - 1; i += 1) {
        const hi = categories[i];
        const lo = categories[i + 1];
        if (value <= hi && value >= lo) {
          return i + (hi - value) / (hi - lo || 1);
        }
      }
      return 0;
    };

    const oiSeries = rows.map((r) =>
      effective === "oi"
        ? (r.oi_gex ?? r.gex)
        : (r.oi_gex ?? r.gex),
    );
    const flowSeries = rows.map((r) => r.flow_gex ?? 0);

    // Default the view to a window around spot: 155 strikes at once is a
    // picture of the whole chain, not of where price is.
    const spotIdx = indexOf(spot);
    const span = Math.max(12, Math.round(categories.length * 0.18));

    const byStrike = new Map<number, Level[]>();
    for (const l of levels) {
      byStrike.set(l.strike, [...(byStrike.get(l.strike) ?? []), l]);
    }
    const merged = [...byStrike.entries()].map(([strike, ls]) => ({
      strike,
      names: ls.map((l) => LEVEL_STYLE[l.kind].text).join(" · "),
      label: ls[0].label,
      // the most structurally significant kind present wins the colour
      colour: LEVEL_STYLE[
        (["max_accel", "max_magnet", "flip", "put_wall", "call_wall",
          "first_pos_above"] as Level["kind"][]).find((k) =>
          ls.some((l) => l.kind === k),
        ) ?? ls[0].kind
      ].colour,
    }));

    const markLines = [
      {
        yAxis: spotIdx,
        lineStyle: { color: "#e5e5e5", width: 1.5, type: "solid" as const },
        label: {
          formatter: `spot ${fmt(spot)}`,
          position: "start" as const,
          color: "#0a0a0a",
          backgroundColor: "#e5e5e5",
          padding: [2, 5],
          borderRadius: 2,
          fontSize: 10,
        },
      },
      // Several levels routinely land on the SAME strike — the call wall is
      // often also the max magnet. Drawn separately they overprint into
      // unreadable mush, so merge by strike into one line and one label.
      ...merged.map((m) => ({
        yAxis: indexOf(m.strike),
        lineStyle: {
          color: m.colour,
          width: 1,
          type: "dashed" as const,
          opacity: 0.8,
        },
        label: {
          formatter: `${m.names}  ${m.label}`,
          position: "insideEndTop" as const,
          color: m.colour,
          fontSize: 10,
          padding: [0, 0, 2, 0],
        },
      })),
    ];

    if (effective === "dex") {
      // EXPOSURE. Deliberately not called a "bid" or a "cushion": those are
      // claims about what dealers do with the exposure, and none is tested.
      return {
        backgroundColor: "transparent",
        grid: { left: 62, right: 34, top: 16, bottom: 28 },
        tooltip: { trigger: "axis" as const, backgroundColor: "#111",
                   borderColor: "#333", textStyle: { color: "#e5e5e5", fontSize: 11 },
                   formatter: (params: any) => {
                     const r = rows[params?.[0]?.dataIndex ?? 0];
                     return r ? `<b>${fmt(r.strike)}</b><div style="color:#888">`
                       + `net delta <span style="color:#e5e5e5">`
                       + `${((r.dex ?? 0) / 1e9).toFixed(2)}B</span></div>` : "";
                   } },
        xAxis: { type: "value" as const,
                 axisLabel: { color: "#666", fontSize: 10,
                              formatter: (v: number) => `${(v / 1e9).toFixed(1)}B` },
                 splitLine: { lineStyle: { color: "#1f1f1f" } } },
        yAxis: { type: "category" as const, inverse: true,
                 data: categories.map(fmt),
                 axisLabel: { color: "#777", fontSize: 10 },
                 axisTick: { show: false }, splitLine: { show: false } },
        dataZoom: [
          { type: "slider" as const, yAxisIndex: 0, filterMode: "none" as const,
            startValue: Math.max(0, Math.round(spotIdx - span)),
            endValue: Math.min(categories.length - 1, Math.round(spotIdx + span)),
            width: 10, right: 4, fillerColor: "#ffffff12", borderColor: "#2a2a2a",
            handleStyle: { color: "#555" }, textStyle: { color: "#666", fontSize: 9 } },
          { type: "inside" as const, yAxisIndex: 0, filterMode: "none" as const },
        ],
        series: [{
          name: "net delta", type: "bar" as const,
          data: rows.map((r) => ({
            value: r.dex ?? 0,
            itemStyle: { color: (r.dex ?? 0) >= 0 ? "#3f8f6f" : "#8f5f3f" },
          })),
          barCategoryGap: "28%",
          markLine: { silent: true, symbol: "none", data: markLines },
        }],
      };
    }

    if (effective === "volume") {
      // Activity, not positioning: unsigned counts, deliberately a different
      // colour family from the signed gamma bars so the two are never read
      // as the same quantity.
      return {
        backgroundColor: "transparent",
        grid: { left: 62, right: 34, top: 16, bottom: 28 },
        tooltip: { trigger: "axis" as const, backgroundColor: "#111",
                   borderColor: "#333", textStyle: { color: "#e5e5e5", fontSize: 11 },
                   formatter: (params: any) => {
                     const r = rows[params?.[0]?.dataIndex ?? 0];
                     return r ? `<b>${fmt(r.strike)}</b>`
                       + `<div style="color:#888">day volume `
                       + `<span style="color:#e5e5e5">${Math.round(r.volume ?? 0).toLocaleString()}</span></div>` : "";
                   } },
        xAxis: { type: "value" as const,
                 axisLabel: { color: "#666", fontSize: 10 },
                 splitLine: { lineStyle: { color: "#1f1f1f" } } },
        yAxis: { type: "category" as const, inverse: true,
                 data: categories.map(fmt),
                 axisLabel: { color: "#777", fontSize: 10 },
                 axisTick: { show: false }, splitLine: { show: false } },
        dataZoom: [
          { type: "slider" as const, yAxisIndex: 0, filterMode: "none" as const,
            startValue: Math.max(0, Math.round(spotIdx - span)),
            endValue: Math.min(categories.length - 1, Math.round(spotIdx + span)),
            width: 10, right: 4, fillerColor: "#ffffff12", borderColor: "#2a2a2a",
            handleStyle: { color: "#555" }, textStyle: { color: "#666", fontSize: 9 } },
          { type: "inside" as const, yAxisIndex: 0, filterMode: "none" as const },
        ],
        series: [{
          name: "day volume", type: "bar" as const,
          data: rows.map((r) => r.volume ?? 0),
          itemStyle: { color: "#7a6f9f" },
          barCategoryGap: "28%",
          markLine: { silent: true, symbol: "none", data: markLines },
        }],
      };
    }

    return {
      backgroundColor: "transparent",
      grid: { left: 62, right: 34, top: 16, bottom: 28 },
      tooltip: {
        trigger: "axis" as const,
        axisPointer: { type: "shadow" as const },
        backgroundColor: "#111",
        borderColor: "#333",
        textStyle: { color: "#e5e5e5", fontSize: 11 },
        formatter: (params: any) => {
          const i = params?.[0]?.dataIndex ?? 0;
          const r = rows[i];
          if (!r) return "";
          const dPts = (r.strike - spot) / divisor;
          const dPct = spot ? ((r.strike - spot) / spot) * 100 : 0;
          const line = (k: string, v: string) =>
            `<div style="display:flex;gap:10px;justify-content:space-between">
               <span style="color:#888">${k}</span><span>${v}</span></div>`;
          return (
            `<b>${fmt(r.strike)}</b>` +
            line("combined", money(r.gex)) +
            line("OI", r.oi_gex === null ? "— (overlay off)" : money(r.oi_gex)) +
            line("flow", r.flow_gex === null ? "— (overlay off)" : money(r.flow_gex)) +
            line("day volume", r.volume === null || r.volume === undefined
              ? "—" : Math.round(r.volume).toLocaleString()) +
            line("from spot", `${dPts >= 0 ? "+" : ""}${dPts.toFixed(0)} pts · ${dPct.toFixed(2)}%`)
          );
        },
      },
      xAxis: {
        type: "value" as const,
        axisLabel: { color: "#666", fontSize: 10, formatter: (v: number) => money(v) },
        splitLine: { lineStyle: { color: "#1f1f1f" } },
      },
      yAxis: {
        type: "category" as const,
        inverse: true,
        data: categories.map(fmt),
        axisLabel: { color: "#777", fontSize: 10 },
        axisTick: { show: false },
        splitLine: { show: false },
      },
      dataZoom: [
        {
          type: "slider" as const,
          yAxisIndex: 0,
          filterMode: "none" as const,
          startValue: Math.max(0, Math.round(spotIdx - span)),
          endValue: Math.min(categories.length - 1, Math.round(spotIdx + span)),
          width: 10,
          right: 4,
          fillerColor: "#ffffff12",
          borderColor: "#2a2a2a",
          handleStyle: { color: "#555" },
          textStyle: { color: "#666", fontSize: 9 },
        },
        { type: "inside" as const, yAxisIndex: 0, filterMode: "none" as const },
      ],
      series:
        effective === "flow" && hasFlow
          ? [
              {
                name: "OI",
                type: "bar" as const,
                stack: "gex",
                data: oiSeries.map((v) => ({
                  value: v,
                  itemStyle: { color: v >= 0 ? OI_COLOUR_POS : OI_COLOUR_NEG },
                })),
                barCategoryGap: "28%",
                markLine: { silent: true, symbol: "none", data: markLines },
              },
              {
                name: "flow",
                type: "bar" as const,
                stack: "gex",
                data: flowSeries.map((v) => ({
                  value: v,
                  itemStyle: { color: v >= 0 ? FLOW_COLOUR_POS : FLOW_COLOUR_NEG },
                })),
              },
            ]
          : [
              {
                name: "OI",
                type: "bar" as const,
                data: oiSeries.map((v) => ({
                  value: v,
                  itemStyle: { color: v >= 0 ? OI_COLOUR_POS : OI_COLOUR_NEG },
                })),
                barCategoryGap: "28%",
                markLine: { silent: true, symbol: "none", data: markLines },
              },
            ],
    };
  }, [rows, levels, spot, effective, divisor]);

  return (
    <section className="rounded-lg border border-neutral-800 bg-neutral-900/40">
      <div className="flex items-center justify-between border-b border-neutral-800 px-4 py-2">
        <h2 className="text-xs font-semibold uppercase tracking-widest text-neutral-500">
          Strike profile
          <span className="ml-2 normal-case tracking-normal text-neutral-600">
            strikes in {unitsLabel}
          </span>
        </h2>
        <div className="flex items-center gap-1 text-xs">
          <Toggle
            active={effective === "flow"}
            disabled={!overlayOn}
            onClick={() => setSource("flow")}
          >
            flow
          </Toggle>
          <Toggle active={effective === "oi"} onClick={() => setSource("oi")}>
            OI
          </Toggle>
          <Toggle active={effective === "dex"} disabled={!hasDex}
                  onClick={() => setSource("dex")}>
            DEX
          </Toggle>
          <Toggle
            active={effective === "volume"}
            disabled={!hasVolume}
            onClick={() => setSource("volume")}
          >
            volume
          </Toggle>
        </div>
      </div>

      <StableChart
        option={option}
        style={{ height: 520 }}
        notMerge
       
      />

      <p className="border-t border-neutral-800 px-4 py-2 text-xs text-neutral-600">
        {effective === "dex" ? (
          <>
            Net dealer delta per strike —{" "}
            <span className="text-neutral-500">exposure, nothing more</span>. It
            is not a &ldquo;mechanical bid&rdquo; or a &ldquo;cushion&rdquo;;
            those describe what dealers would do with it, which is untested here.
          </>
        ) : effective === "volume" ? (
          <>
            Unsigned contracts traded today per strike —{" "}
            <span className="text-neutral-500">activity, not positioning</span>.
            It says nothing about who initiated or which way dealers are hedged.
          </>
        ) : overlayOn && !hasFlow ? (
          <>
            Flow overlay is on but today&rsquo;s classified flow is{" "}
            <span className="text-neutral-500">zero at every strike</span> — the
            market is closed or no prints have been classified yet, so the bars
            are the OI baseline alone. Measured as zero, not missing.
          </>
        ) : overlayOn ? (
          <>
            Stacked: OI baseline + today&rsquo;s classified flow. Flow is signed
            by the tick rule, roughly 75–80% accurate versus NBBO —{" "}
            <span className="text-neutral-500">
              the error is currently unquantified for our data
            </span>
            .
          </>
        ) : (
          <>
            Flow overlay was off for this poll — OI baseline only, which is
            structurally blind to 0DTE. Shown as absent, not as zero.
          </>
        )}
      </p>
    </section>
  );
}

function Toggle({
  active,
  disabled,
  onClick,
  children,
}: {
  active: boolean;
  disabled?: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className={`rounded px-2 py-1 transition-colors ${
        active
          ? "bg-neutral-700 text-neutral-100"
          : disabled
            ? "cursor-not-allowed text-neutral-700"
            : "text-neutral-400 hover:bg-neutral-800"
      }`}
    >
      {children}
    </button>
  );
}
