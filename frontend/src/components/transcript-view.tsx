import { languageTag, wordCount } from "@/lib/format";
import { CopyButton } from "./copy-button";

/** The transcript exactly as the speech service produced it, as text. `lang` lets the browser choose the right font. */
export function TranscriptView({ text, languageCode }: { text: string; languageCode: string }) {
  return (
    <section aria-labelledby="transcript-heading">
      <div className="flex items-baseline justify-between gap-4">
        <h2 id="transcript-heading" className="font-serif text-2xl font-semibold tracking-tight">
          Transcript
        </h2>
        <CopyButton text={text} label="transcript" />
      </div>
      <p className="mt-1 text-sm text-soft">
        {wordCount(text).toLocaleString()} words. It was written automatically, so check names and numbers that matter.
      </p>
      <div lang={languageTag(languageCode)} className="reading mt-5 whitespace-pre-wrap">
        {text}
      </div>
    </section>
  );
}
