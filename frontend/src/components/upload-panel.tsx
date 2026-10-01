"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { useAppConfig } from "@/hooks/use-app-config";
import { useUploadState } from "@/hooks/use-upload-state";
import { formatBytes } from "@/lib/format";
import { isBusy, uploadStore } from "@/lib/upload-store";
import type { Language } from "@/lib/types";
import { formatTypes, problemWith } from "@/lib/validate";
import { WorkingBar } from "./ruler";
import { UploadProgress } from "./upload-progress";
import { Button, ButtonLink, Notice } from "./ui";

function LanguagePicker({
  languages,
  selected,
  max,
  onChange,
}: {
  languages: Language[];
  selected: string[];
  max: number;
  onChange: (codes: string[]) => void;
}) {
  const toggle = (code: string) =>
    onChange(selected.includes(code) ? selected.filter((c) => c !== code) : [...selected, code]);
  return (
    <fieldset className="mt-6">
      <legend className="font-medium">Language spoken</legend>
      <p id="language-help" className="mt-1 text-sm text-soft">
        {selected.length > 1
          ? "With more than one selected, the language is detected automatically."
          : `Choose up to ${max} if the recording mixes languages; the language is then detected automatically.`}
      </p>
      <div className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2.5 sm:grid-cols-4">
        {languages.map(({ code, name }) => {
          const checked = selected.includes(code);
          // at least one stays selected, and at most `max`
          const locked = (checked && selected.length === 1) || (!checked && selected.length >= max);
          return (
            <label key={code} className={`flex items-center gap-2 ${locked && !checked ? "text-soft" : ""}`}>
              <input
                type="checkbox"
                checked={checked}
                disabled={locked}
                onChange={() => toggle(code)}
                aria-describedby="language-help"
                className="h-4 w-4 accent-[var(--ink)]"
              />
              {name}
            </label>
          );
        })}
      </div>
    </fieldset>
  );
}

export function UploadPanel() {
  const config = useAppConfig();
  const upload = useUploadState();
  const router = useRouter();
  const input = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [chosen, setChosen] = useState<string[] | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);

  const languages = chosen ?? [config?.default_language_code ?? "en-IN"];

  // The backend accepted the upload while this panel was watching: the job page shows what happens to it from here.
  // (An upload that finished while the person was on another page is offered as a link instead of yanking them away.)
  const watchedBusy = useRef(false);
  useEffect(() => {
    if (isBusy(upload)) watchedBusy.current = true;
    else if (upload.phase === "done" && watchedBusy.current) {
      router.push(`/uploads/${upload.jobId}`);
      uploadStore.dismiss();
    }
  }, [upload, router]);

  function choose(candidates: FileList | File[] | null | undefined) {
    const files = Array.from(candidates ?? []);
    if (files.length === 0) return;
    if (files.length > 1) return setProblem("Upload one recording at a time.");
    const why = problemWith(files[0], config);
    setProblem(why);
    setFile(why ? null : files[0]);
    uploadStore.dismiss(); // clears an earlier failure from view
  }

  // --- an upload is running in this tab ------------------------------------------------------------------------
  if (upload.phase === "done") {
    return (
      <Notice
        tone="neutral"
        title="Upload complete"
        actions={
          <>
            <ButtonLink href={`/uploads/${upload.jobId}`} variant="primary" onClick={() => uploadStore.dismiss()}>
              Open recording
            </ButtonLink>
            <Button variant="quiet" onClick={() => uploadStore.dismiss()}>
              Upload another
            </Button>
          </>
        }
      >
        <p>{upload.file.name} is uploaded and being processed.</p>
      </Notice>
    );
  }
  if (isBusy(upload)) {
    const name = upload.file.name;
    return (
      <div aria-live="polite">
        <p className="break-all font-serif text-xl">
          {upload.phase === "uploading" ? "Uploading " : upload.phase === "finishing" ? "Checking " : "Preparing "}
          {name}
        </p>
        <div className="mt-4">
          {upload.phase === "uploading" ? (
            <UploadProgress upload={upload} />
          ) : (
            <>
              <WorkingBar label={upload.phase === "finishing" ? "Checking the file" : "Getting ready"} />
              <p className="mt-2 text-sm text-soft">
                {upload.phase === "finishing" ? "Making sure the whole file arrived." : "Asking for a place to put it."}
              </p>
            </>
          )}
        </div>
        {upload.phase === "uploading" && (
          <div className="mt-5 flex flex-wrap items-center gap-x-5 gap-y-2">
            <Button variant="secondary" onClick={() => uploadStore.cancel()}>
              Cancel upload
            </Button>
            <p className="text-sm text-soft">You can open other pages meanwhile. Closing this tab stops the upload.</p>
          </div>
        )}
      </div>
    );
  }

  // --- choosing ------------------------------------------------------------------------------------------------------
  const failed = upload.phase === "failed" ? upload : null;
  return (
    <div>
      {failed && (
        <div className="mb-4">
          <Notice
            title="The upload didn't go through"
            actions={
              <>
                <Button variant="primary" onClick={() => void uploadStore.retry()}>
                  Try again
                </Button>
                <Button
                  variant="quiet"
                  onClick={() => {
                    uploadStore.dismiss();
                    setFile(null);
                  }}
                >
                  Choose another file
                </Button>
              </>
            }
          >
            <p>{failed.message}</p>
            <p className="text-soft">{failed.file.name}</p>
          </Notice>
        </div>
      )}

      {!failed && !file && (
        <div
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragging(false);
            choose(e.dataTransfer.files);
          }}
          className={`rounded-surface border border-dashed px-6 py-9 ${dragging ? "border-ink bg-sheet" : "border-soft/60"}`}
        >
          {/* Dragging only exists with a mouse or trackpad; on a touch screen the only way in is the button. */}
          <p className="font-serif text-xl">
            <span className="pointer-coarse:hidden">Drop a recording here</span>
            <span className="hidden pointer-coarse:inline">Choose a recording</span>
          </p>
          <p className="mt-1 text-soft">
            <span className="pointer-coarse:hidden">or </span>
            <button
              type="button"
              onClick={() => input.current?.click()}
              className="font-medium text-ink underline decoration-2 underline-offset-4"
            >
              choose a file
            </button>{" "}
            from your device.
          </p>
          {config && (
            <p className="mt-5 max-w-prose text-sm text-soft">
              {formatTypes(config)}. Up to {formatBytes(config.max_upload_bytes)}.
            </p>
          )}
          <input
            ref={input}
            type="file"
            tabIndex={-1}
            className="sr-only"
            aria-label="Choose an audio file"
            accept={config ? config.audio_extensions.join(",") : "audio/*,video/mp4"}
            onChange={(e) => {
              choose(e.target.files);
              e.target.value = ""; // choosing the same file again should still fire
            }}
          />
        </div>
      )}

      {problem && (
        <p role="alert" className="mt-3 text-[0.9375rem] text-bad">
          {problem}
        </p>
      )}

      {!failed && file && (
        <div className="rounded-surface border border-rule bg-sheet p-5">
          <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-2">
            <div className="min-w-0">
              <p className="break-all font-medium">{file.name}</p>
              <p className="text-sm text-soft">{formatBytes(file.size)}</p>
            </div>
            <Button variant="quiet" onClick={() => setFile(null)}>
              Choose a different file
            </Button>
          </div>
          {config && (
            <LanguagePicker
              languages={config.languages}
              selected={languages}
              max={config.max_languages}
              onChange={setChosen}
            />
          )}
          <div className="mt-6">
            <Button variant="primary" onClick={() => void uploadStore.start(file, languages.join(","))}>
              Upload
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
