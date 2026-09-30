"use client";

import Link from "next/link";
import { useEffect, useSyncExternalStore } from "react";
import { useAppConfig } from "@/hooks/use-app-config";
import { useNow } from "@/hooks/use-now";
import { useUploadState } from "@/hooks/use-upload-state";
import { useUploads } from "@/hooks/use-uploads";
import { getClientId } from "@/lib/client-id";
import { formatAgo, formatBytes, formatDuration } from "@/lib/format";
import { displayStatus } from "@/lib/status";
import type { UploadListItem } from "@/lib/types";
import { Button, Notice, SkeletonLine, StatusMark } from "./ui";

const DEFAULT_LINK_LIFETIME_SECONDS = 900; // only used until /api/config answers

function Row({ job, now, expiry }: { job: UploadListItem; now: number | null; expiry: number }) {
  const status = displayStatus(job, now ?? 0, expiry);
  return (
    <li>
      <Link href={`/uploads/${job.id}`} className="-mx-3 block rounded-control px-3 py-4 hover:bg-sheet">
        <div className="flex items-baseline justify-between gap-4">
          <p className="min-w-0 break-all font-medium">{job.original_filename}</p>
          <StatusMark tone={status.tone} label={status.label} />
        </div>
        <p className="mt-0.5 flex flex-wrap gap-x-4 text-sm text-soft">
          <span>{formatBytes(job.size_bytes)}</span>
          {job.duration_seconds !== null && <span>{formatDuration(job.duration_seconds)} long</span>}
          {now !== null && (
            <span>
              {job.status === "UPLOADING" ? "Started" : "Uploaded"} {formatAgo(job.created_at, now)}
            </span>
          )}
        </p>
      </Link>
    </li>
  );
}

export function RecordingList() {
  const { data: jobs, error, loading, refresh } = useUploads();
  const config = useAppConfig();
  const upload = useUploadState();
  const now = useNow(30_000);
  // Read from the browser (it cannot be known on the server); true until the browser says otherwise.
  const remembered = useSyncExternalStore(
    () => () => {},
    () => getClientId().persisted,
    () => true,
  );

  // A new upload adds a row the list has not heard of yet: ask again as soon as it exists.
  const uploadJob = "jobId" in upload ? upload.jobId : null;
  useEffect(() => {
    if (uploadJob) refresh();
  }, [uploadJob, refresh]);

  const expiry = config?.upload_url_expires_seconds ?? DEFAULT_LINK_LIFETIME_SECONDS;

  return (
    <section aria-labelledby="recordings-heading">
      <h2 id="recordings-heading" className="font-serif text-2xl font-semibold tracking-tight">
        Your recordings
      </h2>

      {!remembered && (
        <p className="mt-2 text-sm text-soft">
          This browser isn&apos;t keeping history between visits, so these will be gone when you close the page.
        </p>
      )}

      <div className="mt-4">
        {loading && (
          <div className="space-y-5 border-y border-rule py-5" aria-label="Loading your recordings">
            <SkeletonLine className="w-2/3" />
            <SkeletonLine className="w-1/2" />
            <SkeletonLine className="w-3/5" />
          </div>
        )}

        {!loading && !jobs && error && (
          <Notice
            title="Couldn't load your recordings"
            actions={<Button variant="secondary" onClick={refresh}>Try again</Button>}
          >
            <p>{error.message}</p>
          </Notice>
        )}

        {jobs && jobs.length === 0 && (
          <div className="border-y border-rule py-8">
            <p className="font-medium">No recordings yet</p>
            <p className="mt-1 max-w-prose text-soft">
              Upload one above and it will appear here with its transcript and summary. Recordings are kept per
              browser, so uploads from another device or browser won&apos;t show up.
            </p>
          </div>
        )}

        {jobs && jobs.length > 0 && (
          <>
            <ul className="divide-y divide-rule border-y border-rule">
              {jobs.map((job) => (
                <Row key={job.id} job={job} now={now} expiry={expiry} />
              ))}
            </ul>
            {error && (
              <p role="status" className="mt-3 text-sm text-soft">
                Can&apos;t reach the server right now. Showing what was last loaded; this page keeps trying.
              </p>
            )}
          </>
        )}
      </div>
    </section>
  );
}
