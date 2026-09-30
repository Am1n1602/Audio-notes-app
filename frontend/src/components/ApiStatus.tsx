"use client";

import { useEffect, useState } from "react";
import { getHealth } from "@/lib/api";
import type { CheckStatus, Health } from "@/lib/types";

type State =
  | { kind: "loading" }
  | { kind: "loaded"; health: Health }
  | { kind: "error"; message: string };

function Chip({ label, value }: { label: string; value: CheckStatus }) {
  const tone = value === "ok" ? "bg-green-100 text-green-800" : "bg-red-100 text-red-800";
  return (
    <span className={`rounded-full px-3 py-1 text-sm font-medium ${tone}`}>
      {label}: {value}
    </span>
  );
}

// Phase 1 proof that browser -> API works (env wiring + CORS). Replaced by real UI in later phases.
export default function ApiStatus() {
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    getHealth()
      .then((health) => setState({ kind: "loaded", health }))
      .catch((err: unknown) =>
        setState({ kind: "error", message: err instanceof Error ? err.message : "Unknown error" }),
      );
  }, []);

  if (state.kind === "loading") {
    return <p className="text-neutral-500">Checking API…</p>;
  }
  if (state.kind === "error") {
    return (
      <p role="alert" className="rounded-md bg-red-100 px-3 py-2 text-red-800">
        API unreachable: {state.message}
      </p>
    );
  }
  return (
    <div className="flex flex-wrap items-center gap-2" data-testid="api-status">
      <Chip label="API" value={state.health.status === "ok" ? "ok" : "error"} />
      <Chip label="Postgres" value={state.health.db} />
      <Chip label="Redis" value={state.health.redis} />
    </div>
  );
}
