// Timestamps from the API are the server's. A phone or VM clock can be minutes off, so "how long ago" and "is this upload
// old enough to be given up on" are measured against the server's clock: every response says what time it is there.

let offsetMs = 0;

/** Learn the server's time from a response's Date header (ignored if missing or unreadable). */
export function noteServerTime(dateHeader: string | null, receivedAt: number = Date.now()): void {
  const server = dateHeader ? Date.parse(dateHeader) : NaN;
  if (!Number.isNaN(server)) offsetMs = server - receivedAt;
}

/** The current time on the server's clock, in ms since the epoch. Equals the browser's until a response has arrived. */
export function serverNow(): number {
  return Date.now() + offsetMs;
}
