"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "@/lib/api";
import { backoffDelay } from "@/lib/polling";

export interface Polled<T> {
  /** The last successful answer. It is kept while a later check fails, so the screen does not go blank. */
  data: T | undefined;
  /** The most recent failure, cleared by the next success. */
  error: ApiError | null;
  /** Checks that failed in a row: two or more means "it is not just a blip". */
  failures: number;
  /** True until the first answer or failure. */
  loading: boolean;
  /** When the last successful answer arrived (ms since epoch). */
  checkedAt: number | null;
  /** Ask again now. */
  refresh: () => void;
}

const EMPTY = { data: undefined, error: null, failures: 0, loading: true, checkedAt: null };

// These answers will not change by asking again, so asking stops.
const FINAL_ERRORS = new Set([401, 404]);

/**
 * Keeps asking `load` until `delayFor` says stop (returns null). The whole state is rebuilt from the backend on every
 * answer, so a refreshed page just starts asking again. It waits while the tab is hidden, asks at once when the tab
 * comes back or the network returns, and backs off (2x, up to 30 s) while the server is unreachable.
 */
export function usePolled<T>(load: () => Promise<T>, delayFor: (data: T) => number | null, key: string): Polled<T> {
  const [tagged, setTagged] = useState<{ key: string; state: Omit<Polled<T>, "refresh"> } | null>(null);
  const loadRef = useRef(load);
  const delayRef = useRef(delayFor);
  const askNow = useRef<() => void>(() => {});
  useEffect(() => {
    loadRef.current = load;
    delayRef.current = delayFor;
  });

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let inFlight = false;
    let failures = 0;

    const schedule = (ms: number) => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        if (!document.hidden) void run(); // hidden: wait, and resume() asks as soon as the tab is back
      }, ms);
    };

    const run = async () => {
      if (inFlight || cancelled) return;
      inFlight = true;
      clearTimeout(timer);
      try {
        const data = await loadRef.current();
        if (cancelled) return;
        failures = 0;
        setTagged({ key, state: { data, error: null, failures: 0, loading: false, checkedAt: Date.now() } });
        const delay = delayRef.current(data);
        if (delay !== null) schedule(delay);
      } catch (caught) {
        if (cancelled) return;
        failures += 1;
        const error = caught instanceof ApiError ? caught : new ApiError(0, "UNEXPECTED", "Something went wrong.");
        setTagged((previous) => ({
          key,
          state: { ...(previous?.key === key ? previous.state : EMPTY), error, failures, loading: false },
        }));
        if (!FINAL_ERRORS.has(error.status)) schedule(backoffDelay(2500, failures));
      } finally {
        inFlight = false;
      }
    };

    const resume = () => {
      if (!document.hidden) void run();
    };
    askNow.current = () => void run();
    document.addEventListener("visibilitychange", resume);
    window.addEventListener("online", resume);
    void run();

    return () => {
      cancelled = true;
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", resume);
      window.removeEventListener("online", resume);
    };
  }, [key]);

  const refresh = useCallback(() => askNow.current(), []);
  // An answer that belongs to another key (the page moved to a different recording) is not this one's.
  return { ...(tagged?.key === key ? tagged.state : EMPTY), refresh };
}
