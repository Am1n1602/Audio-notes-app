import type { Health } from "./types";

// Public on purpose (bundled into browser code): it is only the API's address, never a secret.
const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL;

export async function getHealth(): Promise<Health> {
  if (!API_BASE) {
    throw new Error("NEXT_PUBLIC_API_BASE_URL is not set");
  }
  const res = await fetch(`${API_BASE}/api/health`, { cache: "no-store" });
  // 503 is a valid answer: the API is up but reports a failing dependency in the body.
  if (res.status !== 200 && res.status !== 503) {
    throw new Error(`API returned HTTP ${res.status}`);
  }
  return (await res.json()) as Health;
}
