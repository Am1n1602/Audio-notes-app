"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { ApiError } from "@/lib/api";
import { startPolling } from "@/lib/poller";

export interface Polled<T> {
  /** The last successful answer. It is kept while a later check fails, so the screen does not go blank. */
  data: T | undefined;
  /** The most recent failure, cleared by the next success. */
  error: ApiError | null;
  /** Checks that failed in a row: two or more means "it is not just a blip". */
  failures: number;
  /** True until the first answer or failure. */
  loading: boolean;
  /** Ask again now. */
  refresh: () => void;
}

const EMPTY = { data: undefined, error: null, failures: 0, loading: true };

/**
 * Keeps asking `load` until `delayFor` says stop (returns null); the loop itself is in lib/poller.ts. The whole state is
 * rebuilt from the backend on every answer, so a refreshed page just starts asking again.
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
    const poller = startPolling<T>({
      load: () => loadRef.current(),
      delayFor: (data) => delayRef.current(data),
      onData: (data) => setTagged({ key, state: { data, error: null, failures: 0, loading: false } }),
      onError: (error, failures) =>
        setTagged((previous) => ({
          key,
          state: { ...(previous?.key === key ? previous.state : EMPTY), error, failures, loading: false },
        })),
      isHidden: () => document.hidden,
      onResume: (resume) => {
        document.addEventListener("visibilitychange", resume);
        window.addEventListener("online", resume);
        return () => {
          document.removeEventListener("visibilitychange", resume);
          window.removeEventListener("online", resume);
        };
      },
    });
    askNow.current = poller.refresh;
    return poller.stop;
  }, [key]);

  const refresh = useCallback(() => askNow.current(), []);
  // An answer that belongs to another key (the page moved to a different recording) is not this one's.
  return { ...(tagged?.key === key ? tagged.state : EMPTY), refresh };
}
