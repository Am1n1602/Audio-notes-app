"use client";

import { useEffect, useState } from "react";
import { getConfig } from "@/lib/api";
import type { AppConfig } from "@/lib/types";

// The limits rarely change, so one answer is shared by every component for the life of the page.
let pending: Promise<AppConfig> | null = null;

/** The backend's real limits, or null until they arrive (or if they cannot: the backend still checks every upload). */
export function useAppConfig(): AppConfig | null {
  const [config, setConfig] = useState<AppConfig | null>(null);
  useEffect(() => {
    let active = true;
    pending ??= getConfig().catch((error) => {
      pending = null; // try again next time the page asks
      throw error;
    });
    pending.then((value) => active && setConfig(value)).catch(() => undefined);
    return () => {
      active = false;
    };
  }, []);
  return config;
}
