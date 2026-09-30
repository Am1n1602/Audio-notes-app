// The browser sends the file straight to storage with a signed PUT. fetch() cannot report upload progress, so this uses
// XMLHttpRequest, whose upload events count the bytes that actually left the browser. Nothing here is estimated.

export interface TransferProgress {
  loaded: number;
  total: number;
}

export type TransferFailure = "network" | "rejected" | "aborted";

export class TransferError extends Error {
  readonly kind: TransferFailure;
  readonly status: number;

  constructor(kind: TransferFailure, status = 0) {
    super(kind === "rejected" ? `upload refused (HTTP ${status})` : `upload ${kind}`);
    this.name = "TransferError";
    this.kind = kind;
    this.status = status;
  }
}

export interface PutOptions {
  onProgress: (progress: TransferProgress) => void;
  signal?: AbortSignal;
  /** Replaceable so the transfer can be tested without a network. */
  createRequest?: () => XMLHttpRequest;
}

export function putFile(
  target: { url: string; headers: Record<string, string> },
  file: Blob,
  { onProgress, signal, createRequest = () => new XMLHttpRequest() }: PutOptions,
): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(new TransferError("aborted"));
    const xhr = createRequest();
    xhr.open("PUT", target.url);
    // Sent exactly as the backend gave them: they are part of the signature, and S3 refuses a different value.
    for (const [name, value] of Object.entries(target.headers)) xhr.setRequestHeader(name, value);

    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress({ loaded: event.loaded, total: event.total });
    };
    xhr.onload = () =>
      xhr.status >= 200 && xhr.status < 300 ? resolve() : reject(new TransferError("rejected", xhr.status));
    xhr.onerror = () => reject(new TransferError("network"));
    xhr.ontimeout = () => reject(new TransferError("network"));
    xhr.onabort = () => reject(new TransferError("aborted"));
    signal?.addEventListener("abort", () => xhr.abort(), { once: true });

    xhr.send(file);
  });
}
