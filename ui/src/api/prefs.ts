import { useCallback, useEffect, useState } from "react";

export const INSTRUMENTS = ["I:SPX", "SPY", "COMPLEX"] as const;
export type InstrumentKey = (typeof INSTRUMENTS)[number];

export const INSTRUMENT_LABEL: Record<InstrumentKey, string> = {
  "I:SPX": "SPX",
  SPY: "SPY",
  COMPLEX: "combined",
};

export type Units = "SPX" | "SPY";

function persisted<T extends string>(key: string, fallback: T, allowed: readonly T[]) {
  return function usePref(): [T, (v: T) => void] {
    const [value, setValue] = useState<T>(() => {
      try {
        const v = window.localStorage.getItem(key) as T | null;
        return v && allowed.includes(v) ? v : fallback;
      } catch {
        return fallback;
      }
    });
    const set = useCallback((v: T) => {
      setValue(v);
      try {
        window.localStorage.setItem(key, v);
      } catch {
        /* private mode — the preference just will not persist */
      }
    }, []);
    useEffect(() => {
      const onStorage = (e: StorageEvent) => {
        if (e.key === key && e.newValue && allowed.includes(e.newValue as T)) {
          setValue(e.newValue as T);
        }
      };
      window.addEventListener("storage", onStorage);
      return () => window.removeEventListener("storage", onStorage);
    }, []);
    return [value, set];
  };
}

/** §10.1: units are a DISPLAY transform. Persisted per user. */
export const useUnits = persisted<Units>("gexbot.units", "SPX", ["SPX", "SPY"]);

/** §10.2: which map is being looked at. A different measurement, not a view. */
export const useInstrument = persisted<InstrumentKey>(
  "gexbot.instrument", "I:SPX", INSTRUMENTS,
);

/**
 * The divisor to apply to every strike on screen.
 *
 * Load-bearing guard against double-scaling: SPY's own map is ALREADY in SPY
 * strikes, so "SPY units" must not divide it again. Only the SPX-scale maps
 * (SPX and the merged complex, which lives on the SPX axis) get divided.
 */
export function strikeDivisor(units: Units, instrument: InstrumentKey): number {
  if (units !== "SPY") return 1;
  return instrument === "SPY" ? 1 : 10;
}
