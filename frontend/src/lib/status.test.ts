import { describe, expect, it } from "vitest";
import {
  displayStatus,
  failureAdvice,
  failureStage,
  isWorking,
  stageStates,
  STATUS_LABEL,
  uploadAbandoned,
} from "./status";
import type { JobStatus } from "./types";

const states = (status: JobStatus, errorCode: string | null = null, hasTranscript = false) =>
  stageStates({ status, error_code: errorCode }, hasTranscript);

describe("the ruler follows the backend's status and nothing else", () => {
  it.each<[JobStatus, string[]]>([
    ["UPLOADING", ["active", "pending", "pending", "pending"]],
    ["UPLOADED", ["done", "waiting", "pending", "pending"]],
    ["QUEUED", ["done", "waiting", "pending", "pending"]],
    ["TRANSCRIBING", ["done", "active", "pending", "pending"]],
    ["SUMMARIZING", ["done", "done", "active", "pending"]],
    ["COMPLETED", ["done", "done", "done", "done"]],
  ])("%s", (status, expected) => expect(states(status)).toEqual(expected));

  it("marks the stage that failed, with what came before it done", () => {
    expect(states("FAILED", "TRANSCRIPTION_FAILED")).toEqual(["done", "failed", "pending", "pending"]);
    expect(states("FAILED", "SUMMARY_UNAVAILABLE", true)).toEqual(["done", "done", "failed", "pending"]);
    expect(states("FAILED", "UPLOAD_SIZE_MISMATCH")).toEqual(["failed", "pending", "pending", "pending"]);
    expect(states("FAILED", "QUEUE_UNAVAILABLE")).toEqual(["done", "failed", "pending", "pending"]);
  });
});

describe("where a failure happened", () => {
  it("a saved transcript means it failed after transcription, whatever the code says", () => {
    expect(failureStage("INTERNAL_ERROR", true)).toBe("summary");
    expect(failureStage("QUEUE_UNAVAILABLE", true)).toBe("summary");
    expect(failureStage("INTERNAL_ERROR", false)).toBe("transcription");
  });
  it("tells a person what they can do next", () => {
    expect(failureAdvice("summary", true)).toMatch(/only repeats the summary/);
    expect(failureAdvice("transcription", true)).toMatch(/Nothing was lost/);
    expect(failureAdvice("transcription", false)).toMatch(/Upload a different file/);
    expect(failureAdvice("summary", false)).toMatch(/transcript below is still here/);
  });
});

describe("labels", () => {
  it("uses the six states people are told about", () => {
    expect(STATUS_LABEL).toMatchObject({
      UPLOADING: "Uploading",
      QUEUED: "Queued",
      TRANSCRIBING: "Transcribing",
      SUMMARIZING: "Summarizing",
      COMPLETED: "Completed",
      FAILED: "Failed",
    });
  });
  it("knows which statuses can still change", () => {
    expect(["UPLOADED", "QUEUED", "TRANSCRIBING", "SUMMARIZING"].every((s) => isWorking(s as JobStatus))).toBe(true);
    expect(isWorking("UPLOADING") || isWorking("COMPLETED") || isWorking("FAILED")).toBe(false);
  });
});

describe("an upload nobody finished", () => {
  const now = Date.parse("2026-09-30T12:00:00Z");
  const created = (minutesAgo: number) => new Date(now - minutesAgo * 60_000).toISOString();
  it("is 'not finished' once the signed link has expired, and still 'uploading' before", () => {
    expect(uploadAbandoned({ status: "UPLOADING", created_at: created(20) }, now, 900)).toBe(true);
    expect(uploadAbandoned({ status: "UPLOADING", created_at: created(5) }, now, 900)).toBe(false);
    expect(uploadAbandoned({ status: "QUEUED", created_at: created(99) }, now, 900)).toBe(false);
  });
  it("is still 'uploading' while this tab is sending it, however long the transfer takes", () => {
    expect(displayStatus({ status: "UPLOADING", created_at: created(45) }, now, 900, true)).toEqual({
      label: "Uploading",
      tone: "working",
    });
  });
  it("shows a tone for each row", () => {
    expect(displayStatus({ status: "UPLOADING", created_at: created(20) }, now, 900)).toEqual({
      label: "Not finished",
      tone: "idle",
    });
    expect(displayStatus({ status: "TRANSCRIBING", created_at: created(1) }, now, 900).tone).toBe("working");
    expect(displayStatus({ status: "COMPLETED", created_at: created(1) }, now, 900).tone).toBe("done");
    expect(displayStatus({ status: "FAILED", created_at: created(1) }, now, 900).tone).toBe("failed");
  });
});
