"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect } from "react";
import { useUploadState } from "@/hooks/use-upload-state";
import { formatBytes } from "@/lib/format";
import { isBusy, uploadStore } from "@/lib/upload-store";

/**
 * An upload only lives as long as this tab: closing or reloading it stops the transfer. So while one is running the
 * browser asks before leaving, and on other pages a slim bar says it is still going and links back to it.
 */
export function UploadGuard() {
  const state = useUploadState();
  const path = usePathname();
  const busy = isBusy(state);

  useEffect(() => {
    if (!busy) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = ""; // required by some browsers to show their own "leave this page?" prompt
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [busy]);

  if (state.phase === "done" && path !== "/") {
    return (
      <div role="status" className="fixed inset-x-0 bottom-0 border-t border-rule bg-sheet px-5 py-3 text-[0.9375rem]">
        <div className="mx-auto flex max-w-[46rem] flex-wrap items-baseline justify-between gap-x-6 gap-y-1">
          <p>
            <span className="font-medium">{state.file.name}</span> is uploaded and being processed.
          </p>
          <span className="flex gap-5">
            <Link
              href={`/uploads/${state.jobId}`}
              onClick={() => uploadStore.dismiss()}
              className="underline decoration-rule decoration-2 underline-offset-4 hover:decoration-ink"
            >
              Open recording
            </Link>
            <button type="button" onClick={() => uploadStore.dismiss()} className="text-soft hover:text-ink">
              Dismiss
            </button>
          </span>
        </div>
      </div>
    );
  }
  if (!busy || path === "/") return null;
  const detail =
    state.phase === "uploading" && state.total > 0
      ? `${Math.floor((state.loaded / state.total) * 100)}% of ${formatBytes(state.total)}`
      : "getting it ready";
  return (
    <div role="status" className="fixed inset-x-0 bottom-0 border-t border-rule bg-sheet px-5 py-3 text-[0.9375rem]">
      <div className="mx-auto flex max-w-[46rem] flex-wrap items-baseline justify-between gap-x-6 gap-y-1">
        <p>
          Uploading <span className="font-medium">{state.file.name}</span>, {detail}.
        </p>
        <Link href="/" className="underline decoration-rule decoration-2 underline-offset-4 hover:decoration-ink">
          Show upload
        </Link>
      </div>
    </div>
  );
}
