import { describe, expect, it } from "vitest";
import type { JobStatus } from "./types";
import { backoffDelay, jobPollDelay, listPollDelay, pollDelay } from "./polling";

const now = Date.parse("2026-09-30T12:00:00Z");
const LIFETIME = 900;
const job = (status: JobStatus, minutesOld = 1) => ({
  status,
  created_at: new Date(now - minutesOld * 60_000).toISOString(),
});

describe("pollDelay", () => {
  it("keeps asking while a job is moving, and stops when it is finished", () => {
    expect(pollDelay("TRANSCRIBING")).toBe(2500);
    expect(pollDelay("QUEUED")).toBe(2500);
    expect(pollDelay("UPLOADING")).toBe(4000);
    expect(pollDelay("COMPLETED")).toBeNull();
    expect(pollDelay("FAILED")).toBeNull();
  });
});

describe("jobPollDelay", () => {
  it("asks about a recent upload, but not one whose link has expired (nobody is sending it any more)", () => {
    expect(jobPollDelay(job("UPLOADING", 5), now, LIFETIME)).toBe(4000);
    expect(jobPollDelay(job("UPLOADING", 20), now, LIFETIME)).toBeNull();
  });
  it("keeps following a job that is being processed, however old", () => {
    expect(jobPollDelay(job("TRANSCRIBING", 600), now, LIFETIME)).toBe(2500);
  });
});

describe("backoffDelay", () => {
  it("doubles after each failed check and never passes 30 seconds", () => {
    expect([0, 1, 2, 3].map((n) => backoffDelay(2500, n))).toEqual([2500, 5000, 10_000, 20_000]);
    expect(backoffDelay(2500, 4)).toBe(30_000);
    expect(backoffDelay(2500, 50)).toBe(30_000);
  });
});

describe("listPollDelay", () => {
  it("stays quiet when nothing in the list can change", () => {
    expect(listPollDelay([], now, LIFETIME)).toBeNull();
    expect(listPollDelay([job("COMPLETED"), job("FAILED")], now, LIFETIME)).toBeNull();
  });
  it("follows the fastest-moving job, a little less eagerly than the open one", () => {
    expect(listPollDelay([job("COMPLETED"), job("TRANSCRIBING")], now, LIFETIME)).toBe(5000);
    expect(listPollDelay([job("UPLOADING")], now, LIFETIME)).toBe(8000);
  });
  it("stops once the only thing left is an abandoned upload, instead of asking for as long as a tab is open", () => {
    expect(listPollDelay([job("COMPLETED"), job("UPLOADING", 60)], now, LIFETIME)).toBeNull();
    expect(listPollDelay([job("UPLOADING", 60), job("QUEUED")], now, LIFETIME)).toBe(5000);
  });
});
