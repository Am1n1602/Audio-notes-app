"use client";

import { useNow } from "@/hooks/use-now";
import { formatAgo } from "@/lib/format";
import { stageStates, STATUS_LABEL } from "@/lib/status";
import type { UploadDetail } from "@/lib/types";
import { Ruler } from "./ruler";

const WHAT_IS_HAPPENING: Partial<Record<UploadDetail["status"], string>> = {
  UPLOADED: "Your recording has arrived and is waiting for its turn.",
  QUEUED: "Your recording has arrived and is waiting for its turn.",
  TRANSCRIBING: "Turning the speech into text. Longer recordings take longer.",
  SUMMARIZING: "The transcript is ready below while the summary is written.",
};

// Steps report in every few seconds, so a job that has been silent this long is worth saying something about.
const QUIET_AFTER_SECONDS = 120;

/**
 * A job the backend is working on. The line under the ruler is the backend's own progress message, and "last activity"
 * is the time the backend last touched the job, so a long recording visibly keeps moving instead of looking frozen.
 */
export function StatusPanel({ job }: { job: UploadDetail }) {
  const now = useNow(1000);
  const quiet = now !== null && (now - new Date(job.updated_at).getTime()) / 1000 > QUIET_AFTER_SECONDS;
  return (
    <section aria-labelledby="status-heading">
      <h2 id="status-heading" className="sr-only">
        Progress
      </h2>
      <Ruler states={stageStates(job, job.transcript !== null)} />
      <div aria-live="polite" className="mt-6">
        <p className="font-serif text-xl">{job.progress_message ?? STATUS_LABEL[job.status]}</p>
        <p className="mt-1 text-soft">{WHAT_IS_HAPPENING[job.status]}</p>
      </div>
      {now !== null && (
        <p className="mt-4 text-sm text-soft">
          Last activity {formatAgo(job.updated_at, now)}.
          {quiet && " This is taking longer than usual. It may still finish, and this page keeps checking."}
        </p>
      )}
      <p className="mt-1 text-sm text-soft">
        You can leave this page. The recording keeps processing and stays in your list.
      </p>
    </section>
  );
}
