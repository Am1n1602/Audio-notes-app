import type { Summary } from "@/lib/types";
import { CopyButton } from "./copy-button";

const GROUPS = [
  { key: "key_points", title: "Key points" },
  { key: "action_items", title: "Action items" },
  { key: "decisions", title: "Decisions" },
  { key: "uncertainties", title: "Unclear points" },
] as const;

/** The summary as plain text, for the clipboard. */
export function summaryText(summary: Summary): string {
  const parts = [summary.overview];
  for (const { key, title } of GROUPS) {
    if (summary[key].length > 0) parts.push(`${title}\n${summary[key].map((item) => `- ${item}`).join("\n")}`);
  }
  return parts.join("\n\n");
}

/** What the model wrote from the transcript. Only the parts it filled in are shown. Rendered as text, never as HTML. */
export function SummaryView({ summary }: { summary: Summary }) {
  return (
    <section aria-labelledby="summary-heading">
      <div className="flex items-baseline justify-between gap-4">
        <h2 id="summary-heading" className="font-serif text-2xl font-semibold tracking-tight">
          Summary
        </h2>
        <CopyButton text={summaryText(summary)} label="summary" />
      </div>
      <p className="mt-1 text-sm text-soft">Written automatically from the transcript. Check anything important against it.</p>
      <p className="reading mt-5">{summary.overview}</p>
      {GROUPS.map(({ key, title }) =>
        summary[key].length > 0 ? (
          <div key={key} className="mt-8">
            <h3 className="font-medium">{title}</h3>
            <ul className="reading mt-2 list-disc space-y-1.5 pl-5">
              {summary[key].map((item, index) => (
                <li key={index}>{item}</li>
              ))}
            </ul>
          </div>
        ) : null,
      )}
    </section>
  );
}
