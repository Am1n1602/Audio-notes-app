"use client";

import { useEffect, useState } from "react";
import { getConfig } from "@/lib/api";
import { backoffDelay } from "@/lib/polling";
import type { AppConfig } from "@/lib/types";

// The limits rarely change, so one answer is shared by every component for the life of the page.
let pending: Promise<AppConfig> | null = null;

/** How long an upload link lives, until /api/config says (the same number the backend's default has). */
export const DEFAULT_LINK_LIFETIME_SECONDS = 900;

/**
 * The backend's real limits, or null until they arrive. If the call fails it asks again, a little later each time, until
 * it works or the page goes away: without the limits there is no language choice, and the backend still checks every upload.
 */
export function useAppConfig(): AppConfig | null {
  const [config, setConfig] = useState<AppConfig | null>(null);
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = (failures: number) => {
      pending ??= getConfig().catch((error) => {
        pending = null; // the next attempt asks again
        throw error;
      });
      pending
        .then((value) => active && setConfig(value))
        .catch(() => {
          if (active) timer = setTimeout(() => load(failures + 1), backoffDelay(2500, failures + 1));
        });
    };
    load(0);
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, []);
  return config;
}

/** Seconds an upload link stays usable: what the backend says, or its default until it has answered. */
export function useLinkLifetime(): number {
  return useAppConfig()?.upload_url_expires_seconds ?? DEFAULT_LINK_LIFETIME_SECONDS;
}
