"use client";

import { useEffect, useRef, useState, type SyntheticEvent } from "react";
import { ApiError, getAudioLink } from "@/lib/api";

type Player =
  | { kind: "loading" }
  | { kind: "ready"; url: string; issuedAt: number }
  | { kind: "unplayable" }
  | { kind: "removed" } // deleted from storage after the retention period
  | { kind: "none" };

// A link that fails sooner than this after it was issued has not lapsed: the browser just cannot play the file.
const LAPSE_AFTER_MS = 60_000;

/**
 * Plays the stored recording through a short-lived signed link. The link is asked for when the page needs it and again
 * whenever it lapses (a long recording, or a page left open), carrying on from the same spot; it is never shown, stored
 * or logged. If the browser cannot play the format, that is said, and once the recording has been deleted from storage
 * (`removed`, or the backend says so when a link is asked for) the page says that instead of showing a dead player.
 */
export function AudioPlayer({ id, removed }: { id: string; removed: boolean }) {
  const [player, setPlayer] = useState<Player>({ kind: "loading" });
  const [attempt, setAttempt] = useState(0); // asking again for a fresh link bumps this
  const resumeAt = useRef(0); // where to carry on from when a fresh link replaces a lapsed one

  useEffect(() => {
    if (removed) return; // nothing to ask for
    let active = true;
    getAudioLink(id)
      .then((link) => active && setPlayer({ kind: "ready", url: link.url, issuedAt: Date.now() }))
      .catch((error) => {
        // Deleted while this page was open: say so. Any other failure (not uploaded, not yours, offline): no player.
        if (active) setPlayer(error instanceof ApiError && error.code === "RECORDING_EXPIRED" ? { kind: "removed" } : { kind: "none" });
      });
    return () => {
      active = false;
    };
  }, [id, attempt, removed]);

  function onError(event: SyntheticEvent<HTMLAudioElement>) {
    // A lapsed link fails the same way as an unplayable format. One that worked for a while has lapsed: ask for a fresh
    // one. One that failed straight away is the format.
    if (player.kind === "ready" && Date.now() - player.issuedAt > LAPSE_AFTER_MS) {
      resumeAt.current = event.currentTarget.currentTime;
      setAttempt((n) => n + 1);
    } else {
      setPlayer({ kind: "unplayable" });
    }
  }

  if (player.kind === "none") return null;
  if (removed || player.kind === "removed") {
    return (
      <section aria-labelledby="audio-heading">
        <h2 id="audio-heading" className="sr-only">
          Recording
        </h2>
        <p className="text-[0.9375rem] text-soft">
          The recording has been deleted from storage, because recordings are not kept for long. The transcript and summary
          are still here.
        </p>
      </section>
    );
  }
  return (
    <section aria-labelledby="audio-heading">
      <h2 id="audio-heading" className="sr-only">
        Recording
      </h2>
      {player.kind === "unplayable" ? (
        <p className="text-[0.9375rem] text-soft">
          This browser couldn&apos;t play the recording. Some formats, like AMR, aren&apos;t supported by browsers. The transcript doesn&apos;t depend on it.
        </p>
      ) : (
        <audio
          key={player.kind === "ready" ? player.url : "loading"}
          controls
          preload="metadata"
          src={player.kind === "ready" ? player.url : undefined}
          onError={player.kind === "ready" ? onError : undefined}
          onLoadedMetadata={(event) => {
            if (resumeAt.current > 0) {
              event.currentTarget.currentTime = resumeAt.current;
              resumeAt.current = 0;
            }
          }}
          className="h-11 w-full"
        >
          Your browser doesn&apos;t support audio playback.
        </audio>
      )}
    </section>
  );
}
