import {
  INSTRUMENT_LABEL,
  INSTRUMENTS,
  strikeDivisor,
  useInstrument,
  useUnits,
} from "../api/prefs";
import { useLive, useNow } from "../api/useLive";
import { ExpiryPanel } from "../components/ExpiryPanel";
import { HealthStrip } from "../components/HealthStrip";
import { RightRail } from "../components/RightRail";
import { StrikeProfile } from "../components/StrikeProfile";
import { TapePanel } from "../components/TapePanel";

/** Screen 1 — Live (spec §2). */
export default function Live() {
  const [instrument, setInstrument] = useInstrument();
  const [units, setUnits] = useUnits();
  const live = useLive(instrument);
  const now = useNow();
  const { poll } = live;
  const divisor = strikeDivisor(units, instrument);

  // The overlay is "on" only when the split is actually present. NULL means
  // it was off for this poll, which is a different fact from zero flow.
  const overlayOn = Boolean(
    poll && poll.oi_net !== null && poll.flow_net !== null,
  );

  return (
    <>
      <HealthStrip live={live} now={now} />

      <div className="flex flex-wrap items-center gap-4 border-b border-neutral-800 px-4 py-2 text-xs">
        <span className="flex items-center gap-1">
          <span className="mr-1 text-neutral-600">instrument</span>
          {INSTRUMENTS.map((k) => (
            <button
              key={k}
              type="button"
              onClick={() => setInstrument(k)}
              className={`rounded px-2 py-1 transition-colors ${
                instrument === k
                  ? "bg-neutral-700 text-neutral-100"
                  : "text-neutral-400 hover:bg-neutral-800"
              }`}
            >
              {INSTRUMENT_LABEL[k]}
            </button>
          ))}
        </span>

        <span className="flex items-center gap-1">
          <span className="mr-1 text-neutral-600">units</span>
          {(["SPX", "SPY"] as const).map((u) => (
            <button
              key={u}
              type="button"
              onClick={() => setUnits(u)}
              className={`rounded px-2 py-1 transition-colors ${
                units === u
                  ? "bg-neutral-700 text-neutral-100"
                  : "text-neutral-400 hover:bg-neutral-800"
              }`}
            >
              {u}
            </button>
          ))}
        </span>

        {instrument === "COMPLEX" && (
          <span className="rounded bg-amber-950/40 px-2 py-1 text-amber-300">
            combined book — a model change, never alerts
          </span>
        )}
        {divisor !== 1 && (
          <span className="text-neutral-600">
            display only: strikes ÷10, gamma unchanged
          </span>
        )}
      </div>

      <main className="p-4">
        {!poll ? (
          <p className="p-8 text-center text-sm text-neutral-500">
            No poll stored for {INSTRUMENT_LABEL[instrument]} — start the engine
            with{" "}
            <code className="text-neutral-400">
              python -m gexbot watch{instrument !== "I:SPX" && " --complex"}
            </code>
            .
          </p>
        ) : !poll.context ? (
          <p className="p-8 text-center text-sm text-neutral-500">
            Waiting for a full snapshot…
          </p>
        ) : (
          <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1fr)_320px]">
            <StrikeProfile
              strikes={poll.strikes}
              levels={poll.context.levels}
              spot={poll.spot}
              overlayOn={overlayOn}
              divisor={divisor}
              unitsLabel={divisor === 1 ? (instrument === "SPY" ? "SPY" : "SPX") : "SPY"}
            />
            <RightRail poll={poll} divisor={divisor} />
            <div className="space-y-4 xl:col-span-2">
              <ExpiryPanel pollId={poll.poll_id} />
              <TapePanel underlying={instrument} />
            </div>
          </div>
        )}
      </main>
    </>
  );
}
