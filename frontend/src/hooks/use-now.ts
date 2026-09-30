"use client";

import { useState, useSyncExternalStore } from "react";

/** A clock that ticks every `everyMs` while something is subscribed to it. */
function createClock(everyMs: number) {
  let value: number | null = null;
  return {
    subscribe(onChange: () => void) {
      value = Date.now();
      const timer = setInterval(() => {
        value = Date.now();
        onChange();
      }, everyMs);
      return () => clearInterval(timer);
    },
    getSnapshot: () => value,
  };
}

/**
 * The current time, refreshed every `everyMs` (fixed for the life of the component). Null on the server and on the very
 * first paint, so the markup the server sent and the browser's first render agree.
 */
export function useNow(everyMs: number): number | null {
  const [clock] = useState(() => createClock(everyMs));
  return useSyncExternalStore(clock.subscribe, clock.getSnapshot, () => null);
}
