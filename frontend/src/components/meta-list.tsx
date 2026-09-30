import { formatBytes, formatDateTime, formatDuration, languageLabel } from "@/lib/format";
import type { Language, UploadDetail } from "@/lib/types";

/** What the recording is, as facts: a definition list, because these are terms and their values. */
export function MetaList({ job, languages }: { job: UploadDetail; languages: Language[] }) {
  // Dates are long, so on a narrow screen they take a whole row instead of wrapping inside half of one.
  const items: { term: string; value: string; wide?: boolean }[] = [
    { term: "Size", value: formatBytes(job.size_bytes) },
    { term: "Language", value: languageLabel(job.language_code, languages) },
  ];
  if (job.duration_seconds !== null) items.push({ term: "Length", value: formatDuration(job.duration_seconds) });
  items.push({ term: "Uploaded", value: formatDateTime(job.created_at), wide: true });
  if (job.completed_at) items.push({ term: "Finished", value: formatDateTime(job.completed_at), wide: true });
  return (
    <dl className="mt-5 grid grid-cols-2 gap-x-8 gap-y-4 sm:grid-cols-3">
      {items.map(({ term, value, wide }) => (
        <div key={term} className={wide ? "col-span-2 sm:col-span-1" : undefined}>
          <dt className="text-sm text-soft">{term}</dt>
          <dd className="font-medium">{value}</dd>
        </div>
      ))}
    </dl>
  );
}
