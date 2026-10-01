"use client";

import { useNow } from "@/hooks/use-now";
import { formatBytes } from "@/lib/format";
import type { UploadState } from "@/lib/upload-store";
import { UploadBar } from "./ruler";

// A transfer that has sent nothing for this long is worth saying so. The number is measured, not guessed.
const STALLED_AFTER_MS = 8000;

type Uploading = Extract<UploadState, { phase: "uploading" }>;

/**
 * The real progress of the upload: a bar from the bytes that left the browser, the byte counts, and, if the bytes stop
 * moving, a plain statement of that with how long it has been. A bar that just sits there looks frozen; this says why.
 */
export function UploadProgress({ upload }: { upload: Uploading }) {
  const now = useNow(1000, Date.now); // measured against upload.movedAt, which is this browser's clock
  const percent = upload.total > 0 ? Math.floor((upload.loaded / upload.total) * 100) : 0;
  const allSent = upload.loaded >= upload.total;
  const quietFor = now === null ? 0 : now - upload.movedAt;
  return (
    <div>
      <UploadBar loaded={upload.loaded} total={upload.total} />
      <p className="mt-2 flex justify-between text-sm text-soft">
        <span>
          <span className="tabular-nums">{percent}</span>%
        </span>
        <span>
          {formatBytes(upload.loaded)} of {formatBytes(upload.total)}
        </span>
      </p>
      <p role="status" className="mt-1 text-sm text-soft">
        {allSent
          ? "Everything has been sent. Waiting for confirmation."
          : quietFor > STALLED_AFTER_MS
            ? `Nothing has been sent for ${Math.round(quietFor / 1000)} seconds. The connection may be slow; the upload keeps trying.`
            : ""}
      </p>
    </div>
  );
}
