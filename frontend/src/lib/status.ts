import type { JobStatus, UploadListItem } from "./types";

// What each backend status means to a person, and what the page should do about it. The backend is the only source of
// truth for status; nothing here invents progress.

export const STATUS_LABEL: Record<JobStatus, string> = {
  UPLOADING: "Uploading",
  UPLOADED: "Queued",
  QUEUED: "Queued",
  TRANSCRIBING: "Transcribing",
  SUMMARIZING: "Summarizing",
  COMPLETED: "Completed",
  FAILED: "Failed",
};

/** A job the backend is working on right now: the page keeps asking until it finishes. */
export function isWorking(status: JobStatus): boolean {
  return status === "UPLOADED" || status === "QUEUED" || status === "TRANSCRIBING" || status === "SUMMARIZING";
}

// --- the ruler: four stages, each one in a state the backend's status decides --------------------------------------

export const STAGES = [
  { key: "upload", label: "Upload" },
  { key: "transcribe", label: "Transcribe" },
  { key: "summarize", label: "Summarize" },
  { key: "ready", label: "Ready" },
] as const;

/** done: finished. active: running now. waiting: next in line and not started. pending: later. failed: stopped here. */
export type StageState = "done" | "active" | "waiting" | "pending" | "failed";

/** Where a failure happened, which decides the heading and what to offer. */
export type FailureStage = "upload" | "start" | "transcription" | "summary";

export function failureStage(errorCode: string | null, hasTranscript: boolean): FailureStage {
  if (errorCode === "UPLOAD_SIZE_MISMATCH") return "upload";
  // A transcript is only ever saved once transcription finished, so a failed job that has one failed after it.
  if (hasTranscript) return "summary";
  if (errorCode === "QUEUE_UNAVAILABLE") return "start";
  return "transcription";
}

export function stageStates(
  job: Pick<UploadListItem, "status" | "error_code">,
  hasTranscript: boolean,
): StageState[] {
  switch (job.status) {
    case "UPLOADING":
      return ["active", "pending", "pending", "pending"];
    case "UPLOADED":
    case "QUEUED":
      return ["done", "waiting", "pending", "pending"];
    case "TRANSCRIBING":
      return ["done", "active", "pending", "pending"];
    case "SUMMARIZING":
      return ["done", "done", "active", "pending"];
    case "COMPLETED":
      return ["done", "done", "done", "done"];
    case "FAILED": {
      const at = { upload: 0, start: 1, transcription: 1, summary: 2 }[failureStage(job.error_code, hasTranscript)];
      return STAGES.map((_, i) => (i < at ? "done" : i === at ? "failed" : "pending"));
    }
  }
}

// --- failures --------------------------------------------------------------------------------------------------------

export const FAILURE_TITLE: Record<FailureStage, string> = {
  upload: "The upload didn't finish correctly",
  start: "Processing didn't start",
  transcription: "Transcription failed",
  summary: "The summary couldn't be written",
};

/** What the person can do next, given whether the backend will accept a retry. */
export function failureAdvice(stage: FailureStage, canRetry: boolean): string {
  if (canRetry) {
    return stage === "summary"
      ? "Your recording was transcribed. Trying again only repeats the summary."
      : "Nothing was lost. Trying again starts from the recording you already uploaded.";
  }
  return stage === "summary"
    ? "The transcript below is still here. The summary can't be retried from this page."
    : "Trying again won't change this. Upload a different file to continue.";
}

// --- the history list ------------------------------------------------------------------------------------------------

export type Tone = "working" | "done" | "failed" | "idle";

/**
 * A row is shown as "not finished" once the signed upload link could no longer have been started. Storage only checks
 * the link when a transfer begins, so one already running can outlive it: the tab doing the sending knows better and
 * says so (displayStatus's `sendingHere`); another tab can only go by the age.
 */
export function uploadAbandoned(
  job: Pick<UploadListItem, "status" | "created_at">,
  now: number,
  uploadUrlExpiresSeconds: number,
): boolean {
  return job.status === "UPLOADING" && now - new Date(job.created_at).getTime() > uploadUrlExpiresSeconds * 1000;
}

export function displayStatus(
  job: Pick<UploadListItem, "status" | "created_at">,
  now: number,
  uploadUrlExpiresSeconds: number,
  sendingHere = false,
): { label: string; tone: Tone } {
  if (!sendingHere && uploadAbandoned(job, now, uploadUrlExpiresSeconds)) return { label: "Not finished", tone: "idle" };
  if (job.status === "COMPLETED") return { label: STATUS_LABEL.COMPLETED, tone: "done" };
  if (job.status === "FAILED") return { label: STATUS_LABEL.FAILED, tone: "failed" };
  return { label: STATUS_LABEL[job.status], tone: "working" };
}
