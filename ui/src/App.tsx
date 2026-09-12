import { useLive, useNow } from "./api/useLive";
import { HealthStrip } from "./components/HealthStrip";
import { RightRail } from "./components/RightRail";
import { StrikeProfile } from "./components/StrikeProfile";

/** Screen 1 — Live (spec §2). */
export default function App() {
  const live = useLive();
  const now = useNow();
  const { poll } = live;

  // The overlay is "on" only when the split is actually present. NULL means
  // it was off for this poll, which is a different fact from zero flow.
  const overlayOn = Boolean(
    poll && poll.oi_net !== null && poll.flow_net !== null,
  );

  return (
    <div className="min-h-screen">
      <HealthStrip live={live} now={now} />

      <main className="p-4">
        {!poll ? (
          <p className="p-8 text-center text-sm text-neutral-500">
            No poll recorded yet — start the engine with{" "}
            <code className="text-neutral-400">python -m gexbot watch</code>.
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
            />
            <RightRail poll={poll} />
          </div>
        )}
      </main>
    </div>
  );
}
