"use client";

import { useEffect, useState } from "react";
import { ApiError, completeUpload } from "@/lib/api";
import { useNow } from "@/hooks/use-now";
import { useUploadState } from "@/hooks/use-upload-state";
import { uploadAbandoned } from "@/lib/status";
import type { UploadDetail } from "@/lib/types";
import { isBusy, uploadStore } from "@/lib/upload-store";
import { WorkingBar } from "./ruler";
import { UploadProgress } from "./upload-progress";
import { Button, ButtonLink, Notice } from "./ui";

type Check = { kind: "checking" } | { kind: "absent" } | { kind: "failed"; message: string };

/**
 * A job still marked "uploading". Either this tab is sending it (show the real progress), or it is not and the backend
 * is asked whether the file actually arrived: if the page was closed right after the last byte, the upload is finished
 * and only the confirmation was lost, which asking (an idempotent call) repairs. Otherwise it says what happened.
 */
export function UnfinishedUpload({
  job,
  linkLifetimeSeconds,
  onConfirmed,
}: {
  job: UploadDetail;
  linkLifetimeSeconds: number;
  onConfirmed: () => void;
}) {
  const upload = useUploadState();
  const now = useNow(5000);
  const sending = isBusy(upload) && "jobId" in upload && upload.jobId === job.id;
  const [check, setCheck] = useState<Check>({ kind: "checking" });

  const [attempt, setAttempt] = useState(0); // "Check again" bumps this to ask the backend once more

  useEffect(() => {
    if (sending) return;
    let active = true;
    completeUpload(job.id)
      .then(() => active && onConfirmed())
      .catch((error: unknown) => {
        if (!active) return;
        if (error instanceof ApiError && error.code === "UPLOAD_NOT_FOUND") setCheck({ kind: "absent" });
        else setCheck({ kind: "failed", message: error instanceof ApiError ? error.message : "Something went wrong." });
      });
    return () => {
      active = false;
    };
  }, [sending, job.id, onConfirmed, attempt]);

  if (sending && isBusy(upload)) {
    return (
      <section aria-live="polite">
        <p className="font-serif text-xl">Uploading from this tab</p>
        <div className="mt-4">
          {upload.phase === "uploading" ? (
            <>
              <UploadProgress upload={upload} />
            </>
          ) : (
            <WorkingBar label="Checking the file" />
          )}
        </div>
        {upload.phase === "uploading" && (
          <div className="mt-5">
            <Button variant="secondary" onClick={() => uploadStore.cancel()}>
              Cancel upload
            </Button>
          </div>
        )}
      </section>
    );
  }

  if (check.kind === "checking") {
    return (
      <section aria-live="polite">
        <p className="font-serif text-xl">Checking whether the file arrived</p>
        <div className="mt-4">
          <WorkingBar label="Checking the file" />
        </div>
      </section>
    );
  }

  if (check.kind === "failed") {
    return (
      <Notice
        title="Couldn't check this upload"
        actions={
          <Button
            variant="secondary"
            onClick={() => {
              setCheck({ kind: "checking" });
              setAttempt((n) => n + 1);
            }}
          >
            Check again
          </Button>
        }
      >
        <p>{check.message}</p>
      </Notice>
    );
  }

  // The file is not in storage. Early on that can just mean another tab is still sending it.
  if (now !== null && uploadAbandoned(job, now, linkLifetimeSeconds)) {
    return (
      <Notice
        title="This upload didn't finish"
        actions={
          <ButtonLink href="/" variant="primary">
            Upload it again
          </ButtonLink>
        }
      >
        <p>The file never finished arriving, so there is nothing to transcribe.</p>
      </Notice>
    );
  }
  return (
    <Notice tone="neutral" title="Waiting for the file">
      <p>The file hasn&apos;t finished arriving. If it is still uploading in another tab, this page updates when it is done.</p>
    </Notice>
  );
}
