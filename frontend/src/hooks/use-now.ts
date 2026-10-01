"use client";

import { useState, useSyncExternalStore } from "react";
import { serverNow } from "@/lib/clock";

/** A clock that ticks every `everyMs` while something is subscribed to it. */
function createClock(everyMs: number, source: () => number) {
  let value: number | null = null;
  return {
    subscribe(onChange: () => void) {
      value = source();
      const timer = setInterval(() => {
        value = source();
        onChange();
      }, everyMs);
      return () => clearInterval(timer);
    },
    getSnapshot: () => value,
  };
}

/**
 * The current time, refreshed every `everyMs` (fixed for the life of the component). By default it is the SERVER's clock,
 * because it is compared with the server's timestamps; pass `Date.now` to time something that happened in this browser.
 * Null on the server and on the very first paint, so the markup the server sent and the browser's first render agree.
 */
export function useNow(everyMs: number, source: () => number = serverNow): number | null {
  const [clock] = useState(() => createClock(everyMs, source));
  return useSyncExternalStore(clock.subscribe, clock.getSnapshot, () => null);
}
