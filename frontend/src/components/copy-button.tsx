"use client";

import { useEffect, useState } from "react";
import { Button } from "./ui";

type Result = "idle" | "copied" | "failed";

export function CopyButton({ text, label }: { text: string; label: string }) {
  const [result, setResult] = useState<Result>("idle");

  useEffect(() => {
    if (result === "idle") return;
    const timer = setTimeout(() => setResult("idle"), 2200);
    return () => clearTimeout(timer);
  }, [result]);

  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setResult("copied");
    } catch {
      setResult("failed"); // blocked by the browser or an insecure page: say so instead of pretending
    }
  }

  return (
    <Button variant="quiet" onClick={copy} aria-label={`Copy ${label}`}>
      <span aria-live="polite">{result === "copied" ? "Copied" : result === "failed" ? "Couldn't copy" : "Copy"}</span>
    </Button>
  );
}
