import * as echarts from "echarts";
import type { CSSProperties } from "react";
import { useEffect, useRef } from "react";

interface Props {
  option: Record<string, unknown>;
  style?: CSSProperties;
  notMerge?: boolean;
}

/**
 * An ECharts container that owns its own lifecycle.
 *
 * This replaces `echarts-for-react` deliberately. That library's init is
 * asynchronous in a way that is easy to miss (`core.js` →
 * `initEchartsInstance`): it calls `echarts.init(ele)`, waits for that
 * instance's `'finished'` event, disposes it, and only then re-inits with the
 * measured size. If the component unmounts and remounts inside that gap — which
 * React StrictMode guarantees, and a parent switching render branches causes in
 * production too — the first mount's still-pending `'finished'` handler
 * disposes the second mount's instance, because both share one `this.ele`.
 *
 * The symptom is specific and thoroughly misleading: the container keeps its
 * `_echarts_instance_` attribute, the `zr-dom` child is present and correctly
 * sized, and there is simply no `<canvas>`. It reads as a data or option fault
 * and is neither — the option is valid, the data is clean, and `resize()` does
 * nothing because there is no live instance to resize. The only direct evidence
 * is one console warning: `[ECharts] Instance … has been disposed`.
 *
 * Keying the element does not help: StrictMode remounts the whole subtree onto
 * the same DOM node, so the key never changes. Init and dispose here are
 * synchronous, so a remount cannot overlap a pending init.
 */
export function StableChart({ option, style, notMerge = true }: Props) {
  const host = useRef<HTMLDivElement | null>(null);
  const chart = useRef<echarts.ECharts | null>(null);

  useEffect(() => {
    if (!host.current) return;
    const instance = echarts.init(host.current);
    chart.current = instance;

    // ResizeObserver rather than the window event: these panels change width
    // when the grid reflows, not only when the window does.
    const ro = new ResizeObserver(() => instance.resize());
    ro.observe(host.current);

    return () => {
      ro.disconnect();
      instance.dispose();        // synchronous — nothing is left pending
      chart.current = null;
    };
  }, []);

  useEffect(() => {
    chart.current?.setOption(option, notMerge);
  }, [option, notMerge]);

  return <div ref={host} style={style} />;
}
