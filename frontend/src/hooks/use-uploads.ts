"use client";

import { getUpload, listUploads } from "@/lib/api";
import { listPollDelay, pollDelay } from "@/lib/polling";
import type { UploadDetail, UploadListItem } from "@/lib/types";
import { usePolled, type Polled } from "./use-polled";

/** One recording, kept current while it is being processed. */
export function useUpload(id: string): Polled<UploadDetail> {
  return usePolled(() => getUpload(id), (job) => pollDelay(job.status), `upload:${id}`);
}

/** This browser's recordings, kept current while any of them can still change. */
export function useUploads(): Polled<UploadListItem[]> {
  return usePolled(listUploads, (jobs) => listPollDelay(jobs.map((job) => job.status)), "uploads");
}
