"use client";

import { useState } from "react";
import { ApiError, retryUpload } from "@/lib/api";
import { failureAdvice, failureStage, FAILURE_TITLE } from "@/lib/status";
import type { UploadDetail } from "@/lib/types";
import { Button, ButtonLink, Notice } from "./ui";

/** A failed job: where it failed, the backend's own message, what can be done, and a reference if help is needed. */
export function FailurePanel({ job, onRetried }: { job: UploadDetail; onRetried: () => void }) {
  const [retrying, setRetrying] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const stage = failureStage(job.error_code, job.transcript !== null);

  async function retry() {
    setRetrying(true);
    setProblem(null);
    try {
      await retryUpload(job.id);
      onRetried(); // the job is moving again: the page starts following it
    } catch (error) {
      setProblem(error instanceof ApiError ? error.message : "Something went wrong. Please try again.");
    } finally {
      setRetrying(false);
    }
  }

  return (
    <Notice
      title={FAILURE_TITLE[stage]}
      actions={
        job.can_retry ? (
          <Button variant="primary" onClick={retry} disabled={retrying}>
            {retrying ? "Starting…" : stage === "summary" ? "Try the summary again" : "Try again"}
          </Button>
        ) : stage !== "summary" ? (
          <ButtonLink href="/" variant="primary">
            Upload a different file
          </ButtonLink>
        ) : undefined
      }
    >
      <p>{job.error_message ?? "Something went wrong while processing this recording."}</p>
      <p className="text-soft">{failureAdvice(stage, job.can_retry)}</p>
      {problem && <p className="text-bad">{problem}</p>}
      <p className="pt-1 text-sm text-soft">
        Reference <span className="select-all break-all">{job.id}</span>
      </p>
    </Notice>
  );
}
