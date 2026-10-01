"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, type ReactNode } from "react";
import { useUploadState } from "@/hooks/use-upload-state";
import { formatBytes } from "@/lib/format";
import { isBusy, uploadStore } from "@/lib/upload-store";

const linkClass = "underline decoration-rule decoration-2 underline-offset-4 hover:decoration-ink";

function Bar({ children }: { children: ReactNode }) {
  return (
    <div role="status" className="fixed inset-x-0 bottom-0 border-t border-rule bg-sheet px-5 py-3 text-[0.9375rem]">
      <div className="mx-auto flex max-w-[46rem] flex-wrap items-baseline justify-between gap-x-6 gap-y-1">
        {children}
      </div>
    </div>
  );
}

/**
 * An upload only lives as long as this tab: closing or reloading it stops the transfer. So while one is running the
 * browser asks before leaving, and on other pages a slim bar says it is still going, finished or failed, and links back.
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

  // Already looking at the recording that just finished uploading: there is nothing to point to, so clear it.
  const onItsPage = state.phase === "done" && path === `/uploads/${state.jobId}`;
  useEffect(() => {
    if (onItsPage) uploadStore.dismiss();
  }, [onItsPage]);

  if (path === "/" || onItsPage) return null; // the upload panel on the home page says it all

  if (state.phase === "done") {
    return (
      <Bar>
        <p>
          <span className="font-medium">{state.file.name}</span> is uploaded and being processed.
        </p>
        <span className="flex gap-5">
          <Link href={`/uploads/${state.jobId}`} onClick={() => uploadStore.dismiss()} className={linkClass}>
            Open recording
          </Link>
          <button type="button" onClick={() => uploadStore.dismiss()} className="text-soft hover:text-ink">
            Dismiss
          </button>
        </span>
      </Bar>
    );
  }
  if (state.phase === "failed") {
    return (
      <Bar>
        <p>
          <span className="font-medium">{state.file.name}</span> didn&apos;t finish uploading.
        </p>
        <span className="flex gap-5">
          <Link href="/" className={linkClass}>
            See what happened
          </Link>
          <button type="button" onClick={() => uploadStore.dismiss()} className="text-soft hover:text-ink">
            Dismiss
          </button>
        </span>
      </Bar>
    );
  }
  if (!isBusy(state)) return null;
  const detail =
    state.phase === "uploading" && state.total > 0
      ? `${Math.floor((state.loaded / state.total) * 100)}% of ${formatBytes(state.total)}`
      : "getting it ready";
  return (
    <Bar>
      <p>
        Uploading <span className="font-medium">{state.file.name}</span>, {detail}.
      </p>
      <Link href="/" className={linkClass}>
        Show upload
      </Link>
    </Bar>
  );
}
