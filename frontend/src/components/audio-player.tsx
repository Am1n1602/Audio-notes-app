"use client";

import { useEffect, useState } from "react";
import { getAudioLink } from "@/lib/api";

type Player = { kind: "loading" } | { kind: "ready"; url: string } | { kind: "unplayable" } | { kind: "none" };

/**
 * Plays the stored recording through a short-lived signed link. The link is asked for when the page needs it and
 * again if it lapses; it is never shown, stored or logged. If the browser cannot play the format, that is said.
 */
export function AudioPlayer({ id }: { id: string }) {
  const [player, setPlayer] = useState<Player>({ kind: "loading" });
  const [attempt, setAttempt] = useState(0); // asking again for a fresh link bumps this

  useEffect(() => {
    let active = true;
    getAudioLink(id)
      .then((link) => active && setPlayer({ kind: "ready", url: link.url }))
      .catch(() => active && setPlayer({ kind: "none" })); // no link (not uploaded, not yours, offline): no player
    return () => {
      active = false;
    };
  }, [id, attempt]);

  function onError() {
    // A lapsed link fails the same way as an unplayable format. Ask for a fresh link once; if that fails too, it is the format.
    if (attempt === 0) setAttempt(1);
    else setPlayer({ kind: "unplayable" });
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
          className="h-11 w-full"
        >
          Your browser doesn&apos;t support audio playback.
        </audio>
      )}
    </section>
  );
}
