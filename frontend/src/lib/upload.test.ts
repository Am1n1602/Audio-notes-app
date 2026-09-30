import { describe, expect, it } from "vitest";
import { putFile, TransferError } from "./upload";

/** Just enough of XMLHttpRequest to drive putFile: the test decides what the "network" does. */
class FakeRequest {
  method = "";
  url = "";
  headers: Record<string, string> = {};
  sent: Blob | null = null;
  status = 0;
  aborted = false;
  upload: { onprogress: ((e: { lengthComputable: boolean; loaded: number; total: number }) => void) | null } = {
    onprogress: null,
  };
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  ontimeout: (() => void) | null = null;
  onabort: (() => void) | null = null;

  open(method: string, url: string) {
    this.method = method;
    this.url = url;
  }
  setRequestHeader(name: string, value: string) {
    this.headers[name] = value;
  }
  send(body: Blob) {
    this.sent = body;
  }
  abort() {
    this.aborted = true;
    this.onabort?.();
  }
  progress(loaded: number, total: number, lengthComputable = true) {
    this.upload.onprogress?.({ lengthComputable, loaded, total });
  }
  finish(status: number) {
    this.status = status;
    this.onload?.();
  }
}

const target = { url: "https://bucket.example/uploads/1/audio.wav?sig=x", headers: { "Content-Type": "audio/wav" } };
const file = new Blob(["RIFF...."], { type: "audio/wav" });

function start(signal?: AbortSignal) {
  const request = new FakeRequest();
  const progress: Array<[number, number]> = [];
  const done = putFile(target, file, {
    onProgress: (p) => progress.push([p.loaded, p.total]),
    signal,
    createRequest: () => request as unknown as XMLHttpRequest,
  });
  return { request, progress, done };
}

describe("putFile", () => {
  it("PUTs the file to the signed URL with exactly the headers it was given", async () => {
    const { request, done } = start();
    request.finish(200);
    await done;
    expect(request.method).toBe("PUT");
    expect(request.url).toBe(target.url);
    expect(request.headers).toEqual({ "Content-Type": "audio/wav" });
    expect(request.sent).toBe(file);
  });

  it("reports the bytes that really left the browser, and skips events with no known total", async () => {
    const { request, progress, done } = start();
    request.progress(1024, 8192);
    request.progress(3000, 0, false); // the browser does not know the total: nothing honest to report
    request.progress(8192, 8192);
    request.finish(200);
    await done;
    expect(progress).toEqual([
      [1024, 8192],
      [8192, 8192],
    ]);
  });

  it("does not call an upload a success just because the request ended: storage must say 2xx", async () => {
    const { request, done } = start();
    request.finish(403);
    await expect(done).rejects.toMatchObject({ name: "TransferError", kind: "rejected", status: 403 });
  });

  it("reports a dropped connection as a network failure", async () => {
    const { request, done } = start();
    request.onerror?.();
    await expect(done).rejects.toMatchObject({ kind: "network" });
  });

  it("treats a timeout as a network failure too", async () => {
    const { request, done } = start();
    request.ontimeout?.();
    await expect(done).rejects.toMatchObject({ kind: "network" });
  });

  it("can be cancelled, which aborts the request", async () => {
    const controller = new AbortController();
    const { request, done } = start(controller.signal);
    controller.abort();
    await expect(done).rejects.toMatchObject({ kind: "aborted" });
    expect(request.aborted).toBe(true);
  });

  it("does not even start when it was cancelled before it began", async () => {
    const controller = new AbortController();
    controller.abort();
    const { request, done } = start(controller.signal);
    await expect(done).rejects.toBeInstanceOf(TransferError);
    expect(request.sent).toBeNull();
  });
});
