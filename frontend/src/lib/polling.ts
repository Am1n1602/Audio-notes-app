import { isWorking } from "./status";
import type { JobStatus } from "./types";

// The page re-asks the backend while a job is moving and stops when it is not. These are the only timing rules.

const WORKING_MS = 2500; // a job moves between stages every few seconds; this keeps the screen current without hammering
const UPLOADING_MS = 4000; // another tab or device is finishing the upload
const MAX_BACKOFF_MS = 30_000;

/** Milliseconds until the next check of ONE job, or null when it is finished and nothing will change. */
export function pollDelay(status: JobStatus): number | null {
  if (isWorking(status)) return WORKING_MS;
  if (status === "UPLOADING") return UPLOADING_MS;
  return null;
}

/** After a failed check, wait longer each time (2x) up to 30 s, so a server that is down is not hammered. */
export function backoffDelay(baseMs: number, consecutiveFailures: number): number {
  return Math.min(MAX_BACKOFF_MS, baseMs * 2 ** Math.max(0, consecutiveFailures));
}

/** The history list only needs re-checking while something in it can still change. */
export function listPollDelay(statuses: JobStatus[]): number | null {
  const delays = statuses.map(pollDelay).filter((d): d is number => d !== null);
  return delays.length ? Math.min(...delays) * 2 : null; // the list can be a little less eager than the open job
}
