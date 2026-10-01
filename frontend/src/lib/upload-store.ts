import { ApiError, completeUpload, initiateUpload } from "./api";
import { putFile, TransferError, type TransferProgress } from "./upload";

// The upload in progress in THIS tab. It lives outside React so it keeps going when the person moves to another page,
// and so the whole flow can be tested with fakes. After a reload nothing here survives (the bytes are gone): the
// backend still has the job, and the job page explains what happened.

export interface FileInfo {
  name: string;
  size: number;
}

export type UploadState =
  | { phase: "idle" }
  | { phase: "preparing"; file: FileInfo } // asking the backend for a place to put the file
  | { phase: "uploading"; file: FileInfo; jobId: string; loaded: number; total: number; movedAt: number } // movedAt: when bytes last left the browser
  | { phase: "finishing"; file: FileInfo; jobId: string } // bytes are in storage; asking the backend to verify them
  | { phase: "done"; file: FileInfo; jobId: string } // the backend accepted it: the job page takes over
  | { phase: "failed"; file: FileInfo; message: string; jobId?: string }; // jobId: the file did arrive, only the check failed

export type BusyState = Extract<UploadState, { phase: "preparing" | "uploading" | "finishing" }>;

export const isBusy = (state: UploadState): state is BusyState =>
  state.phase === "preparing" || state.phase === "uploading" || state.phase === "finishing";

export interface UploadDeps {
  initiate: typeof initiateUpload;
  put: typeof putFile;
  complete: typeof completeUpload;
  wait: (ms: number) => Promise<void>;
  now: () => number;
}

const realDeps: UploadDeps = {
  initiate: initiateUpload,
  put: putFile,
  complete: completeUpload,
  wait: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  now: () => Date.now(),
};

const PROGRESS_EVERY_MS = 100; // bytes arrive in a flood; the screen only needs to follow at a readable pace
const FINISH_RETRY_MS = [1500, 3000, 5000];
// Completing right after the last byte can briefly say "not there yet", or storage can hiccup: both are worth a retry.
const RETRYABLE_FINISH = new Set(["UPLOAD_NOT_FOUND", "STORAGE_UNAVAILABLE", "NETWORK"]);
// If the check still fails after those retries for one of these, the bytes are in storage and only the answer was lost, so
// "Try again" asks the check again instead of sending the whole file a second time.
const FILE_ARRIVED = new Set(["STORAGE_UNAVAILABLE", "NETWORK"]);

export function createUploadStore(deps: UploadDeps = realDeps) {
  let state: UploadState = { phase: "idle" };
  let current: { file: File; languageCode: string; abort: AbortController } | null = null;
  const listeners = new Set<() => void>();

  const set = (next: UploadState) => {
    state = next;
    listeners.forEach((listener) => listener());
  };

  async function finish(jobId: string, file: FileInfo): Promise<void> {
    set({ phase: "finishing", file, jobId });
    for (let attempt = 0; ; attempt++) {
      try {
        await deps.complete(jobId);
        return set({ phase: "done", file, jobId });
      } catch (error) {
        if (!(error instanceof ApiError)) throw error;
        // The backend could not hand the job to its queue, but it recorded the failure against this job and offers a
        // retry there, so the job page is the right place to land.
        if (error.code === "QUEUE_UNAVAILABLE") return set({ phase: "done", file, jobId });
        const wait = FINISH_RETRY_MS[attempt];
        if (!RETRYABLE_FINISH.has(error.code) || wait === undefined) {
          const arrived = FILE_ARRIVED.has(error.code) ? { jobId } : {};
          return set({ phase: "failed", file, message: error.message, ...arrived });
        }
        await deps.wait(wait);
      }
    }
  }

  async function confirm(jobId: string, file: FileInfo): Promise<void> {
    try {
      await finish(jobId, file);
    } catch {
      set({ phase: "failed", file, message: "Something went wrong. Please try again." });
    }
  }

  async function run(file: File, languageCode: string): Promise<void> {
    const info: FileInfo = { name: file.name, size: file.size };
    const abort = new AbortController();
    current = { file, languageCode, abort };
    set({ phase: "preparing", file: info });

    let jobId: string;
    let target: Awaited<ReturnType<UploadDeps["initiate"]>>["upload"];
    try {
      const started = await deps.initiate(info, languageCode);
      jobId = started.id;
      target = started.upload;
    } catch (error) {
      const message = error instanceof ApiError ? error.message : "Something went wrong. Please try again.";
      return set({ phase: "failed", file: info, message });
    }

    let lastShown = 0;
    const onProgress = ({ loaded, total }: TransferProgress) => {
      const at = deps.now();
      if (loaded < total && at - lastShown < PROGRESS_EVERY_MS) return;
      lastShown = at;
      set({ phase: "uploading", file: info, jobId, loaded, total, movedAt: at });
    };
    set({ phase: "uploading", file: info, jobId, loaded: 0, total: file.size, movedAt: deps.now() });
    try {
      await deps.put(target, file, { onProgress, signal: abort.signal });
    } catch (error) {
      if (error instanceof TransferError && error.kind === "aborted") return set({ phase: "idle" });
      const message =
        error instanceof TransferError && error.kind === "rejected"
          ? "The upload link expired or was refused. Try again to get a new one."
          : "The upload was interrupted. Check your connection and try again.";
      return set({ phase: "failed", file: info, message });
    }

    await confirm(jobId, info);
  }

  return {
    subscribe(listener: () => void) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    getState: () => state,
    /** Start uploading. Ignored while another upload is in progress. */
    start(file: File, languageCode: string): Promise<void> {
      return isBusy(state) ? Promise.resolve() : run(file, languageCode);
    },
    /** Stop an upload in progress. The backend keeps the job (it shows as not finished); no bytes are needed again. */
    cancel() {
      current?.abort.abort();
    },
    /** Try again: ask for the check once more if the file had arrived, else send it again (a new upload). */
    retry(): Promise<void> {
      if (!current || state.phase !== "failed") return Promise.resolve();
      const { jobId, file } = state;
      return jobId ? confirm(jobId, file) : run(current.file, current.languageCode);
    },
    /** Clear a finished or failed upload from view. */
    dismiss() {
      if (!isBusy(state)) {
        current = null;
        set({ phase: "idle" });
      }
    },
  };
}

export const uploadStore = createUploadStore();
