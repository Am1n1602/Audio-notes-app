import { describe, expect, it, vi } from "vitest";
import { ApiError } from "./api";
import { createUploadStore, type UploadDeps, type UploadState } from "./upload-store";
import { TransferError } from "./upload";
import type { UploadDetail } from "./types";

const TARGET = { url: "https://bucket.example/x", method: "PUT" as const, headers: { "Content-Type": "audio/wav" }, expires_in_seconds: 900 };
const file = new File([new Uint8Array(1000)], "meeting.wav", { type: "audio/wav" });

function setup(overrides: Partial<UploadDeps> = {}) {
  const waits: number[] = [];
  let clock = 0;
  const deps = {
    initiate: vi.fn().mockResolvedValue({ id: "job-1", status: "UPLOADING", upload: TARGET }),
    put: vi.fn().mockImplementation(async (_t, f: File, o) => {
      o.onProgress({ loaded: f.size, total: f.size });
    }),
    complete: vi.fn().mockResolvedValue({ id: "job-1" } as UploadDetail),
    wait: vi.fn().mockImplementation(async (ms: number) => void waits.push(ms)),
    now: () => (clock += 1000),
    ...overrides,
  };
  const store = createUploadStore(deps as unknown as UploadDeps);
  const phases: string[] = [];
  store.subscribe(() => phases.push(store.getState().phase));
  return { store, deps, phases, waits };
}

const apiError = (code: string, message = "backend words", status = 409) => new ApiError(status, code, message);

describe("a normal upload", () => {
  it("moves through preparing, uploading and finishing, and ends with the job the backend accepted", async () => {
    const { store, deps, phases } = setup();
    await store.start(file, "hi-IN");
    expect(phases).toEqual(["preparing", "uploading", "uploading", "finishing", "done"]);
    expect(store.getState()).toEqual({ phase: "done", file: { name: "meeting.wav", size: 1000 }, jobId: "job-1" });
    expect(deps.initiate).toHaveBeenCalledWith({ name: "meeting.wav", size: 1000 }, "hi-IN");
    expect(deps.put).toHaveBeenCalledWith(TARGET, file, expect.objectContaining({ onProgress: expect.any(Function) }));
    expect(deps.complete).toHaveBeenCalledWith("job-1");
  });

  it("shows real progress from the bytes sent", async () => {
    const seen: UploadState[] = [];
    const { store } = setup({
      put: vi.fn().mockImplementation(async (_t, f: File, o) => {
        o.onProgress({ loaded: 250, total: f.size });
        o.onProgress({ loaded: 1000, total: f.size });
      }),
    });
    store.subscribe(() => seen.push(store.getState()));
    await store.start(file, "en-IN");
    const uploading = seen.filter((s) => s.phase === "uploading");
    expect(uploading.map((s) => (s.phase === "uploading" ? s.loaded : -1))).toEqual([0, 250, 1000]);
  });

  it("records when bytes last moved, so the page can say when they stop", async () => {
    const seen: UploadState[] = [];
    const { store } = setup({
      put: vi.fn().mockImplementation(async (_t, f: File, o) => {
        o.onProgress({ loaded: 300, total: f.size });
        o.onProgress({ loaded: 700, total: f.size });
      }),
    });
    store.subscribe(() => seen.push(store.getState()));
    await store.start(file, "en-IN");
    const moved = seen.flatMap((state) => (state.phase === "uploading" ? [state.movedAt] : []));
    expect(moved.length).toBeGreaterThanOrEqual(3); // start, then each progress event
    expect(moved).toEqual([...moved].sort((a, b) => a - b)); // never goes backwards
    expect(moved.at(-1)).toBeGreaterThan(moved[0]); // and moves forward when bytes arrive
  });

  it("does not claim success until the backend has verified the file", async () => {
    let release: () => void = () => {};
    const { store } = setup({ complete: vi.fn().mockReturnValue(new Promise<UploadDetail>((r) => (release = () => r({} as UploadDetail)))) });
    const running = store.start(file, "en-IN");
    await vi.waitFor(() => expect(store.getState().phase).toBe("finishing")); // all bytes sent, still not done
    release();
    await running;
    expect(store.getState().phase).toBe("done");
  });

  it("ignores a second start while one is running", async () => {
    const { store, deps } = setup();
    const first = store.start(file, "en-IN");
    await store.start(new File(["x"], "other.wav"), "en-IN");
    await first;
    expect(deps.initiate).toHaveBeenCalledTimes(1);
  });
});

describe("when starting fails", () => {
  it("shows the backend's own message (too large, wrong type) and never starts uploading", async () => {
    const { store, deps } = setup({ initiate: vi.fn().mockRejectedValue(apiError("FILE_TOO_LARGE", "The file is too large. The maximum size is 2.0 GB.", 413)) });
    await store.start(file, "en-IN");
    expect(store.getState()).toMatchObject({ phase: "failed", message: "The file is too large. The maximum size is 2.0 GB." });
    expect(deps.put).not.toHaveBeenCalled();
  });

  it("does not show an unexpected error's details", async () => {
    const { store } = setup({ initiate: vi.fn().mockRejectedValue(new TypeError("Cannot read properties of undefined (reading 'upload')")) });
    await store.start(file, "en-IN");
    expect(store.getState()).toMatchObject({ phase: "failed", message: "Something went wrong. Please try again." });
  });
});

describe("when the transfer fails", () => {
  it("says the upload was interrupted and does not ask the backend to verify a file that never arrived", async () => {
    const { store, deps } = setup({ put: vi.fn().mockRejectedValue(new TransferError("network")) });
    await store.start(file, "en-IN");
    expect(store.getState()).toMatchObject({ phase: "failed", message: expect.stringMatching(/interrupted/) });
    expect(deps.complete).not.toHaveBeenCalled();
  });

  it("explains an expired link without naming the storage service or the HTTP status", async () => {
    const { store } = setup({ put: vi.fn().mockRejectedValue(new TransferError("rejected", 403)) });
    await store.start(file, "en-IN");
    const state = store.getState();
    expect(state).toMatchObject({ phase: "failed", message: expect.stringMatching(/expired or was refused/) });
    expect(JSON.stringify(state)).not.toMatch(/403|S3|amazon/i);
  });

  it("goes back to idle when the person cancels", async () => {
    const { store } = setup({
      put: vi.fn().mockImplementation((_t, _f, o) => new Promise((_, reject) => o.signal.addEventListener("abort", () => reject(new TransferError("aborted"))))),
    });
    const running = store.start(file, "en-IN");
    await vi.waitFor(() => expect(store.getState().phase).toBe("uploading"));
    store.cancel();
    await running;
    expect(store.getState()).toEqual({ phase: "idle" });
  });
});

describe("finishing", () => {
  it("retries while the file has not shown up in storage yet, then succeeds", async () => {
    const complete = vi.fn()
      .mockRejectedValueOnce(apiError("UPLOAD_NOT_FOUND"))
      .mockRejectedValueOnce(apiError("STORAGE_UNAVAILABLE", "x", 503))
      .mockResolvedValue({} as UploadDetail);
    const { store, waits } = setup({ complete });
    await store.start(file, "en-IN");
    expect(store.getState().phase).toBe("done");
    expect(complete).toHaveBeenCalledTimes(3);
    expect(waits).toEqual([1500, 3000]);
  });

  it("gives up after a few tries and says so, instead of waiting forever", async () => {
    const complete = vi.fn().mockRejectedValue(apiError("UPLOAD_NOT_FOUND", "The file has not arrived in storage."));
    const { store } = setup({ complete });
    await store.start(file, "en-IN");
    expect(complete).toHaveBeenCalledTimes(4);
    expect(store.getState()).toMatchObject({ phase: "failed", message: "The file has not arrived in storage." });
  });

  it("does not retry a refusal that retrying cannot fix", async () => {
    const complete = vi.fn().mockRejectedValue(apiError("UPLOAD_SIZE_MISMATCH", "The uploaded file does not match the size that was declared."));
    const { store } = setup({ complete });
    await store.start(file, "en-IN");
    expect(complete).toHaveBeenCalledTimes(1);
    expect(store.getState()).toMatchObject({ phase: "failed" });
  });

  it("hands over to the job page when the queue was down: the job records it and offers Retry there", async () => {
    const { store } = setup({ complete: vi.fn().mockRejectedValue(apiError("QUEUE_UNAVAILABLE", "x", 503)) });
    await store.start(file, "en-IN");
    expect(store.getState()).toEqual({ phase: "done", file: { name: "meeting.wav", size: 1000 }, jobId: "job-1" });
  });
});

describe("after a failure", () => {
  it("retry uploads the same file again as a new upload", async () => {
    const put = vi.fn().mockRejectedValueOnce(new TransferError("network")).mockImplementation(async () => {});
    const { store, deps } = setup({ put });
    await store.start(file, "hi-IN");
    expect(store.getState().phase).toBe("failed");
    await store.retry();
    expect(deps.initiate).toHaveBeenCalledTimes(2);
    expect(deps.initiate).toHaveBeenLastCalledWith({ name: "meeting.wav", size: 1000 }, "hi-IN");
    expect(store.getState().phase).toBe("done");
  });

  it("when only the final check failed, retry asks the check again instead of sending the whole file a second time", async () => {
    const complete = vi.fn().mockRejectedValue(apiError("NETWORK", "Can't reach the server.", 0));
    const { store, deps } = setup({ complete });
    await store.start(file, "hi-IN");
    expect(store.getState()).toMatchObject({ phase: "failed", jobId: "job-1" }); // the file did arrive
    complete.mockResolvedValue({} as UploadDetail);
    await store.retry();
    expect(deps.initiate).toHaveBeenCalledTimes(1);
    expect(deps.put).toHaveBeenCalledTimes(1);
    expect(store.getState()).toEqual({ phase: "done", file: { name: "meeting.wav", size: 1000 }, jobId: "job-1" });
  });

  it("when the file never showed up in storage, retry does send it again", async () => {
    const { store, deps } = setup({ complete: vi.fn().mockRejectedValue(apiError("UPLOAD_NOT_FOUND")) });
    await store.start(file, "en-IN");
    expect(store.getState()).not.toHaveProperty("jobId");
    await store.retry();
    expect(deps.initiate).toHaveBeenCalledTimes(2);
  });

  it("dismiss clears the screen, but never while an upload is running", async () => {
    const { store } = setup();
    await store.start(file, "en-IN");
    store.dismiss();
    expect(store.getState()).toEqual({ phase: "idle" });
  });
});
