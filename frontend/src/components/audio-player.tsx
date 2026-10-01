"use client";

import { useEffect, useRef, useState, type SyntheticEvent } from "react";
import { getAudioLink } from "@/lib/api";

type Player =
  | { kind: "loading" }
  | { kind: "ready"; url: string; issuedAt: number }
  | { kind: "unplayable" }
  | { kind: "none" };

// A link that fails sooner than this after it was issued has not lapsed: the browser just cannot play the file.
const LAPSE_AFTER_MS = 60_000;

/**
 * Plays the stored recording through a short-lived signed link. The link is asked for when the page needs it and again
 * whenever it lapses (a long recording, or a page left open), carrying on from the same spot; it is never shown, stored
 * or logged. If the browser cannot play the format, that is said.
 */
export function AudioPlayer({ id }: { id: string }) {
  const [player, setPlayer] = useState<Player>({ kind: "loading" });
  const [attempt, setAttempt] = useState(0); // asking again for a fresh link bumps this
  const resumeAt = useRef(0); // where to carry on from when a fresh link replaces a lapsed one

  useEffect(() => {
    let active = true;
    getAudioLink(id)
      .then((link) => active && setPlayer({ kind: "ready", url: link.url, issuedAt: Date.now() }))
      .catch(() => active && setPlayer({ kind: "none" })); // no link (not uploaded, not yours, offline): no player
    return () => {
      active = false;
    };
  }, [id, attempt]);

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
