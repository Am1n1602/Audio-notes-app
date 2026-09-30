import { describe, expect, it } from "vitest";
import { backoffDelay, listPollDelay, pollDelay } from "./polling";

describe("pollDelay", () => {
  it("keeps asking while a job is moving, and stops when it is finished", () => {
    expect(pollDelay("TRANSCRIBING")).toBe(2500);
    expect(pollDelay("QUEUED")).toBe(2500);
    expect(pollDelay("UPLOADING")).toBe(4000);
    expect(pollDelay("COMPLETED")).toBeNull();
    expect(pollDelay("FAILED")).toBeNull();
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
    expect(listPollDelay([])).toBeNull();
    expect(listPollDelay(["COMPLETED", "FAILED"])).toBeNull();
  });
  it("follows the fastest-moving job, a little less eagerly than the open one", () => {
    expect(listPollDelay(["COMPLETED", "TRANSCRIBING"])).toBe(5000);
    expect(listPollDelay(["UPLOADING"])).toBe(8000);
  });
});
