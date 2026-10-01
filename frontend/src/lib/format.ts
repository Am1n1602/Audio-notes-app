import type { Language } from "./types";

// Sizes use 1024, the same base the backend uses in its own messages ("The maximum size is 2.0 GB").
export function formatBytes(bytes: number): string {
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)}\u00a0GB`;
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)}\u00a0MB`;
  if (bytes >= 1024) return `${Math.round(bytes / 1024)}\u00a0KB`;
  return `${bytes}\u00a0B`;
}

/** "1 min 59 s", "2 h 4 min": what a person says, not a clock face. */
export function formatDuration(totalSeconds: number): string {
  const seconds = Math.round(totalSeconds);
  if (seconds < 60) return `${seconds} s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return seconds % 60 === 0 ? `${minutes} min` : `${minutes} min ${seconds % 60} s`;
  const hours = Math.floor(minutes / 60);
  return minutes % 60 === 0 ? `${hours} h` : `${hours} h ${minutes % 60} min`;
}

/** A span in the words a sentence needs: "2 hours", "1 hour", "90 minutes". (formatDuration is the compact form.) */
export function formatSpan(totalSeconds: number): string {
  const minutes = Math.max(1, Math.round(totalSeconds / 60));
  if (minutes % 60 === 0) {
    const hours = minutes / 60;
    return hours === 1 ? "1 hour" : `${hours} hours`;
  }
  return minutes === 1 ? "1 minute" : `${minutes} minutes`;
}

/** How long ago, coarsely. `now` is a parameter so the result can be tested and re-computed on a timer. */
export function formatAgo(iso: string, now: number): string {
  const seconds = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds} s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.floor(hours / 24);
  return days === 1 ? "yesterday" : `${days} days ago`;
}

export function formatDateTime(iso: string): string {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(iso));
}

/** "hi-IN,en-IN" -> "Hindi, English (India)". A code the backend did not list is shown as it is. */
export function languageLabel(codes: string, languages: Language[]): string {
  return codes
    .split(",")
    .map((code) => languages.find((l) => l.code === code.trim())?.name ?? code.trim())
    .join(", ");
}

/** The BCP 47 tag for one language ("hi-IN"), so the browser picks the right font and shaping. Undefined for several. */
export function languageTag(codes: string): string | undefined {
  return codes.includes(",") ? undefined : codes.trim() || undefined;
}

export function wordCount(text: string): number {
  const trimmed = text.trim();
  return trimmed ? trimmed.split(/\s+/).length : 0;
}
