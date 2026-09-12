import { useEffect, useState } from "react";

import { api, isAbort } from "../api/client";
import type { ChainQuote, ChainView } from "../api/types";
import { money, price } from "../components/format";

const SYMBOLS = ["SPX", "XSP"] as const;

/**
 * Entry surface — the chain view (exit-manager spec §4).
 *
 * Deliberately minimal: strike, bid/ask, mid, spread width, and the GEX at
 * that strike. Enough to choose a contract and nothing more. No chart — the
 * operator uses TradingView — and no order ticket here: the API is read-only
 * (CLAUDE.md §13), and in shadow mode the operator enters through his own
 * broker anyway. Positions are registered with `gexbot paper open`.
 *
 * Spread is given its own column rather than being left implicit in bid/ask.
 * The §2.5 ladder crosses the book once per tranche, and on XSP that cost is
 * not a rounding error — measured live, XSP call spreads of 5.68 and 6.75 sat
 * beside SPX's uniform 0.4-0.5 on the same strikes.
 */
export default function Chain() {
  const [symbol, setSymbol] = useState<(typeof SYMBOLS)[number]>("SPX");
  const [dte, setDte] = useState(3);
  const [strikes, setStrikes] = useState(5);
  const [view, setView] = useState<ChainView | null>(null);
  // The XSP chain takes seconds to fetch (2,900 contracts, plus the SPX chain
  // for its gamma). Until it lands the table still holds the previous symbol's
  // rows, and showing those under the new symbol's header would be exactly the
  // stale-as-current failure this project keeps finding. The banner below says
  // which symbol the rows actually belong to.
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    const ac = new AbortController();
    setLoading(true);
    setError(null);
    api.chain(symbol, dte, strikes, ac.signal)
      .then((v) => { setView(v); setLoading(false); })
      .catch((e) => {
        if (isAbort(e)) return;
        setError(String(e));
        setLoading(false);
      });
    return () => ac.abort();
  }, [symbol, dte, strikes]);

  return (
    <div className="space-y-4 p-4">
      <div className="flex flex-wrap items-center gap-3 text-xs">
        <h1 className="text-sm font-semibold uppercase tracking-widest text-neutral-400">
          Chain
        </h1>
        <span className="flex items-center gap-1">
          {SYMBOLS.map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => setSymbol(s)}
              className={`rounded px-2 py-1 transition-colors ${
                symbol === s
                  ? "bg-neutral-700 text-neutral-100"
                  : "text-neutral-400 hover:bg-neutral-800"
              }`}
            >
              {s}
            </button>
          ))}
        </span>
        <Num label="DTE" value={dte} onChange={setDte} min={0} max={60} />
        <Num label="± strikes" value={strikes} onChange={setStrikes} min={1} max={25} />
        {view?.spot != null && (
          <span className="ml-auto text-neutral-400">
            spot <span className="text-neutral-100">{price(view.spot, symbol === "XSP" ? 2 : 0)}</span>
            {view.symbol === "XSP" && view.spx_equivalent_spot != null && (
              <span className="text-neutral-600">
                {" "}= SPX {price(view.spx_equivalent_spot)}
              </span>
            )}
            {view.expiry && (
              <span className="text-neutral-600">
                {" "}· {view.expiry} ({view.dte}d)
              </span>
            )}
          </span>
        )}
      </div>

      {error && (
        <p className="rounded border border-rose-900 bg-rose-950/40 p-3 text-sm text-rose-200">
          {error}
        </p>
      )}
      {loading && !view && (
        <p className="p-6 text-sm text-neutral-500">Fetching the chain…</p>
      )}
      {loading && view && view.symbol !== symbol && (
        <p className="rounded border border-amber-600/50 bg-amber-950/30 px-3 py-2 text-xs text-amber-200">
          Loading {symbol}… the table below is still {view.symbol}.
        </p>
      )}
      {view?.detail && (
        <p className="rounded border border-neutral-800 bg-neutral-900/40 p-4 text-sm text-neutral-500">
          {view.detail}
        </p>
      )}

      {view && view.rows.length > 0 && (
        <section
          className={`overflow-x-auto rounded-lg border border-neutral-800 bg-neutral-900/40 ${
            view.symbol !== symbol ? "opacity-40" : ""
          }`}
        >
          <table className="w-full text-xs tabular-nums">
            <thead className="text-neutral-500">
              <tr className="border-b border-neutral-800">
                <th colSpan={4} className="px-2 py-1 text-left font-normal text-sky-400/70">
                  calls
                </th>
                <th colSpan={2} className="px-2 py-1 text-center font-normal">
                  strike
                </th>
                <th colSpan={4} className="px-2 py-1 text-right font-normal text-rose-400/70">
                  puts
                </th>
              </tr>
              <tr className="border-b border-neutral-800">
                {["bid", "ask", "mid", "spr"].map((h) => (
                  <th key={`c${h}`} className="px-2 py-1 text-left font-normal">{h}</th>
                ))}
                <th className="px-2 py-1 text-center font-normal">strike</th>
                <th className="px-2 py-1 text-center font-normal">GEX</th>
                {["spr", "mid", "bid", "ask"].map((h) => (
                  <th key={`p${h}`} className="px-2 py-1 text-right font-normal">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {view.rows.map((r) => (
                <tr
                  key={r.strike}
                  className={`border-b border-neutral-800/40 ${
                    r.atm ? "bg-neutral-800/50 text-neutral-100" : ""
                  }`}
                >
                  <Q q={r.call} field="bid" />
                  <Q q={r.call} field="ask" />
                  <Q q={r.call} field="mid" />
                  <Spread q={r.call} />
                  <td className="px-2 py-0.5 text-center font-medium">
                    {price(r.strike, view.symbol === "XSP" ? 1 : 0)}
                    {view.symbol === "XSP" && (
                      <span className="ml-1 text-neutral-600">
                        ({price(r.spx_equivalent)})
                      </span>
                    )}
                  </td>
                  <td
                    className={`px-2 py-0.5 text-center ${
                      r.gex === null
                        ? "text-neutral-700"
                        : r.gex >= 0
                          ? "text-sky-400"
                          : "text-rose-400"
                    }`}
                  >
                    {r.gex === null ? "—" : money(r.gex)}
                  </td>
                  <Spread q={r.put} align="right" />
                  <Q q={r.put} field="mid" align="right" />
                  <Q q={r.put} field="bid" align="right" />
                  <Q q={r.put} field="ask" align="right" />
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {view && view.rows.length > 0 && (
        <div className="space-y-1 rounded-lg border border-neutral-800 bg-neutral-900/40 p-4 text-xs text-neutral-600">
          <p>
            <span className="text-neutral-400">Levels</span> ({view.symbol} scale):{" "}
            {Object.entries(view.levels)
              .filter(([, v]) => v != null)
              .map(([k, v]) => `${k.replace(/_/g, " ")} ${price(v as number, view.symbol === "XSP" ? 1 : 0)}`)
              .join(" · ") || "none in window"}
            {view.net_gex != null && (
              <> · net {money(view.net_gex)}</>
            )}
          </p>
          {view.gex_source === "spx_map" && (
            <p className="text-amber-300">
              GEX shown is the <span className="font-semibold">SPX map</span> at
              the equivalent level — XSP option snapshots publish no greeks, so
              a profile cannot be built from its own chain. XSP is one tenth of
              the same index by contract definition, so ×10 is exact here.
            </p>
          )}
          <p>
            <span className="text-neutral-400">Spread matters.</span> Scaling
            out crosses the book once per tranche (§2.6), and the shadow record
            counts that cost per tranche rather than only the P&amp;L headline.
          </p>
          <p>
            Read-only. Register a position you entered yourself with{" "}
            <code className="text-neutral-500">gexbot paper open</code> — the
            bot shadows the exit and places nothing.
          </p>
        </div>
      )}
    </div>
  );
}

function Q({ q, field, align = "left" }: {
  q: ChainQuote | null; field: "bid" | "ask" | "mid"; align?: "left" | "right";
}) {
  const v = q?.[field];
  return (
    <td className={`px-2 py-0.5 text-${align} ${v == null ? "text-neutral-700" : ""}`}>
      {v == null ? "—" : v.toFixed(2)}
    </td>
  );
}

/** Wide spreads are flagged, not just printed — on XSP they are the cost the
 *  ladder pays repeatedly. */
function Spread({ q, align = "left" }: { q: ChainQuote | null; align?: "left" | "right" }) {
  const s = q?.spread;
  const wide = s != null && q?.mid != null && q.mid > 0 && s / q.mid > 0.15;
  return (
    <td
      className={`px-2 py-0.5 text-${align} ${
        s == null ? "text-neutral-700" : wide ? "text-amber-400" : "text-neutral-500"
      }`}
    >
      {s == null ? "—" : s.toFixed(2)}
    </td>
  );
}

function Num({ label, value, onChange, min, max }: {
  label: string; value: number; onChange: (n: number) => void;
  min: number; max: number;
}) {
  return (
    <label className="flex items-center gap-1 text-neutral-600">
      {label}
      <input
        type="number"
        value={value}
        min={min}
        max={max}
        onChange={(e) => onChange(Math.max(min, Math.min(max, Number(e.target.value) || min)))}
        className="w-16 rounded border border-neutral-700 bg-neutral-900 px-2 py-1 text-neutral-200"
      />
    </label>
  );
}
