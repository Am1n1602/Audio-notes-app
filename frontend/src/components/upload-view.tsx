"use client";

import Link from "next/link";
import { useEffect } from "react";
import { useAppConfig, useLinkLifetime } from "@/hooks/use-app-config";
import { useUpload } from "@/hooks/use-uploads";
import { isWorking } from "@/lib/status";
import { AudioPlayer } from "./audio-player";
import { FailurePanel } from "./failure-panel";
import { MetaList } from "./meta-list";
import { StatusPanel } from "./status-panel";
import { SummaryView } from "./summary-view";
import { TranscriptView } from "./transcript-view";
import { UnfinishedUpload } from "./unfinished-upload";
import { Button, ButtonLink, Loading, Notice, SkeletonLine, StaleNote, StatusMark } from "./ui";

const BackLink = () => (
  <Link href="/" className="text-sm text-soft underline decoration-rule decoration-2 underline-offset-4 hover:text-ink">
    All recordings
  </Link>
);

/** One recording. Everything shown is rebuilt from the backend on every check, so a reload loses nothing. */
export function UploadView({ id }: { id: string }) {
  const { data: job, error, loading, failures, refresh } = useUpload(id);
  const config = useAppConfig();
  const linkLifetime = useLinkLifetime();
  const name = job?.original_filename;

  useEffect(() => {
    if (name) document.title = `${name} · Audio Notes`;
  }, [name]);

  if (loading) {
    return (
      <Loading label="Loading the recording" className="space-y-6">
        <SkeletonLine className="w-24" />
        <SkeletonLine className="h-9 w-3/4" />
        <SkeletonLine className="w-1/2" />
      </Loading>
    );
  }

  if (!job) {
    if (error?.status === 404 || error?.status === 401) {
      return (
        <div className="space-y-6">
          <BackLink />
          <Notice
            tone="neutral"
            title="We can't find that recording"
            actions={
              <ButtonLink href="/" variant="primary">
                Go to your recordings
              </ButtonLink>
            }
          >
            <p>
              Recordings are kept per browser. This one may have been uploaded in a different browser or before site
              data was cleared.
            </p>
          </Notice>
        </div>
      );
    }
    return (
      <div className="space-y-6">
        <BackLink />
        <Notice
          title="Couldn't load this recording"
          actions={
            <Button variant="secondary" onClick={refresh}>
              Try again
            </Button>
          }
        >
          <p>{error?.message ?? "Something went wrong."} This page keeps trying.</p>
        </Notice>
      </div>
    );
  }

  const languages = config?.languages ?? [];
  const hasTranscript = job.transcript !== null;
  return (
    <article className="space-y-12">
      <header>
        <BackLink />
        <h1 className="mt-5 break-all font-serif text-3xl font-semibold leading-tight tracking-tight">{name}</h1>
        {job.status === "COMPLETED" && (
          <p className="mt-2">
            <StatusMark tone="done" label="Completed" />
          </p>
        )}
        <MetaList job={job} languages={languages} />
      </header>

      {failures >= 2 && <StaleNote className="-mt-6" />}

      {job.status === "UPLOADING" && (
        <UnfinishedUpload job={job} linkLifetimeSeconds={linkLifetime} onConfirmed={refresh} />
      )}
      {isWorking(job.status) && <StatusPanel job={job} />}
      {job.status === "FAILED" && <FailurePanel job={job} onRetried={refresh} />}

      {job.status !== "UPLOADING" && <AudioPlayer id={job.id} />}
      {job.summary && <SummaryView summary={job.summary} />}
      {hasTranscript && <TranscriptView text={job.transcript ?? ""} languageCode={job.language_code} />}
    </article>
  );
}
