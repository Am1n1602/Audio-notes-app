"use client";

import { getUpload, listUploads } from "@/lib/api";
import { serverNow } from "@/lib/clock";
import { jobPollDelay, listPollDelay } from "@/lib/polling";
import type { UploadDetail, UploadListItem } from "@/lib/types";
import { useLinkLifetime } from "./use-app-config";
import { usePolled, type Polled } from "./use-polled";

/** One recording, kept current while it is being processed. */
export function useUpload(id: string): Polled<UploadDetail> {
  const linkLifetime = useLinkLifetime();
  return usePolled(() => getUpload(id), (job) => jobPollDelay(job, serverNow(), linkLifetime), `upload:${id}`);
}

/** This browser's recordings, kept current while any of them can still change. */
export function useUploads(): Polled<UploadListItem[]> {
  const linkLifetime = useLinkLifetime();
  return usePolled(listUploads, (jobs) => listPollDelay(jobs, serverNow(), linkLifetime), "uploads");
}
