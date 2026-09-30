"use client";

import { useEffect } from "react";
import { Button, ButtonLink, Notice } from "@/components/ui";

// Anything the page did not expect lands here instead of a blank screen. The details stay in the console, not on screen.
export default function ErrorPage({ error, retry }: { error: Error & { digest?: string }; retry: () => void }) {
  useEffect(() => {
    console.error(error);
  }, [error]);

  return (
    <Notice
      title="Something went wrong on this page"
      actions={
        <>
          <Button variant="primary" onClick={() => retry()}>
            Try again
          </Button>
          <ButtonLink href="/" variant="quiet">
            Go to your recordings
          </ButtonLink>
        </>
      }
    >
      <p>Your recordings are safe. Try again, or reload the page.</p>
    </Notice>
  );
}
