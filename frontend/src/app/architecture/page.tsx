import type { Metadata } from "next";
import { ArchitectureDiagram } from "@/components/architecture-diagram";
import { GITHUB_URL } from "@/lib/site";

export const metadata: Metadata = {
  title: "How it works",
  description: "How a recording becomes a transcript and a summary, and why it is built this way.",
};

function Section({ id, title, children }: { id: string; title: string; children: React.ReactNode }) {
  return (
    <section aria-labelledby={id} className="mt-14">
      <h2 id={id} className="font-serif text-2xl font-semibold tracking-tight">
        {title}
      </h2>
      <div className="reading mt-4 space-y-4">{children}</div>
    </section>
  );
}

export default function ArchitecturePage() {
  return (
    <article>
      <h1 className="font-serif text-3xl font-semibold tracking-tight sm:text-4xl">How it works</h1>
      <p className="reading mt-4">
        Audio Notes turns a voice recording into a transcript and a short summary. This page explains how, and why each
        part is built the way it is. The{" "}
        <a
          href={GITHUB_URL}
          target="_blank"
          rel="noopener noreferrer"
          className="font-medium underline decoration-2 underline-offset-4"
        >
          source code is on GitHub
        </a>
        .
      </p>

      <div className="mt-10">
        <ArchitectureDiagram />
      </div>

      <Section id="flow" title="From recording to notes">
        <ol className="list-decimal space-y-3 pl-5">
          <li>
            The browser asks the API to start an upload. The API checks the file type and size, records a new job in
            Postgres, and returns a signed link to a private storage bucket.
          </li>
          <li>
            The browser sends the file straight to storage with that link. The file never passes through our servers.
          </li>
          <li>
            When the last byte is sent, the browser asks the API to finish. The API checks that the file really is in
            storage at the size that was declared, and only then queues the job.
          </li>
          <li>The job waits in a queue until a worker is free.</li>
          <li>
            The worker takes one short step and records the result before doing anything else. A step is small enough
            that restarting the worker loses nothing.
          </li>
          <li>
            The worker gives Gnani a short-lived link to the file, not the file itself, and asks for a transcript. It
            checks back every 10 seconds until Gnani is done, then saves the transcript.
          </li>
          <li>
            The worker sends the transcript to a language model, which writes a short summary in a fixed structure. A
            long transcript is summarised in parts and the parts are merged.
          </li>
          <li>
            The page asks the API for the job&apos;s state every few seconds and shows whatever the database says. A
            refreshed page rebuilds itself from the same answer.
          </li>
        </ol>
      </Section>

      <Section id="upload" title="Why the file goes straight to storage">
        <p>
          Recordings can be large, and a server that relays them has to hold every connection open for as long as the
          upload takes. So the API never touches the audio: it signs a link, and the browser sends the bytes to the
          bucket itself. The signature covers the file type and exact size, so storage refuses anything that differs
          from what was checked, and the link expires after 15 minutes. Uploads are limited to 2&nbsp;GB.
        </p>
        <p>
          The bucket is private. The only ways to reach a file are a signed link the API hands to you for playback, and
          the short-lived link given to Gnani for transcription.
        </p>
      </Section>

      <Section id="batch" title="Why batch transcription, and how long recordings are handled">
        <p>
          Gnani&apos;s synchronous speech endpoint accepts about a minute of audio. Its batch service takes recordings up
          to four hours long and fetches the file from a link, which is what makes the direct-to-storage design work.
          A transcription can take a while, so nothing waits for it.
        </p>
        <p>
          Long transcripts are also too big for one request to a language model, and the model has per-minute limits.
          So the transcript is split at word boundaries, each part is summarised in its own step, and the part summaries
          are merged a few at a time. Progress is saved after every step, so a restart carries on from the next part.
        </p>
      </Section>

      <Section id="background" title="What happens now and what happens later">
        <p>
          Only quick work happens while you wait: checking the file type and size, creating the job, signing a link, and
          confirming the upload arrived. Transcribing and summarising happen in the background, and no request waits for
          them. You can close the page and come back; the job keeps going.
        </p>
      </Section>

      <Section id="progress" title="How progress is shown">
        <p>
          The upload bar is the number of bytes that have actually left your browser. After that, the speech service
          does not say how far along it is, so the page shows which stage is running (upload, transcribe, summarize,
          ready) and never a made-up percentage. To show that a long job is still alive, it also shows when the job was
          last touched by the worker.
        </p>
      </Section>

      <Section id="failures" title="When something goes wrong">
        <ul className="list-disc space-y-3 pl-5">
          <li>
            A temporary problem, such as the speech service being busy, is retried a few times with longer pauses, and
            polling gives up after two hours. A problem that retrying cannot fix, such as a corrupt file or silence, is
            shown straight away with a plain explanation.
          </li>
          <li>
            Every failure says which stage failed and what you can do. If retrying can help there is a button for it;
            if not, the page says so.
          </li>
          <li>
            If only the summary fails, the transcript is kept and readable, and retrying repeats the summary without
            transcribing the recording again.
          </li>
          <li>
            A transcription is never started twice for the same attempt. The worker claims the job first, saves
            Gnani&apos;s job id the moment it has it, and does not blindly repeat a request whose outcome it cannot know.
          </li>
        </ul>
      </Section>

      <Section id="data" title="What is stored">
        <p>
          Postgres holds one row per recording: the file name, size, type and language, the job&apos;s status, the
          transcription job&apos;s id and status, the transcript, the summary, any error, and timestamps. The audio
          itself stays in storage, never in the database, and is deleted automatically two hours after the upload begins: a timer is
          set for each upload, and a cleanup that runs once a day removes any that were missed. The transcript and summary are kept.
        </p>
        <p>
          There are no accounts. Your browser makes up a random id and keeps it; the server stores only a hash of it and
          shows you the recordings that match. That keeps visitors&apos; uploads private from each other, but it is not a
          login: clearing this site&apos;s data, or using another browser, starts an empty history.
        </p>
      </Section>

      <Section id="deployment" title="Where it runs">
        <p>
          The web app, the API and the step runner are separate processes that share only Postgres, the queue and
          storage. This deployment runs them as follows.
        </p>
        <ul className="list-disc space-y-3 pl-5">
          <li>The web app is a Next.js site on Vercel.</li>
          <li>
            The API (<code>api</code>, public) and the step runner (<code>steps</code>, private) are two Google Cloud
            Run services built from one image, in Mumbai. Only the queue can call <code>steps</code>.
          </li>
          <li>
            The queue is a Google Cloud Tasks queue, <code>job-steps</code>. It calls <code>steps</code> when each step
            is due and tries again if a step does not answer, so there is no always-on worker: nothing runs, or costs
            anything, while nobody is using it.
          </li>
          <li>
            A Google Cloud Scheduler job calls <code>steps</code> once a day, at 03:00 India time, to delete any recording that
            is past its time. It is a safety net: each upload also has its own deletion scheduled in the queue.
          </li>
          <li>The database is Neon Postgres in Singapore, the closest region it offers to Mumbai.</li>
          <li>Recordings are in a private AWS S3 bucket in Mumbai. Your browser uploads to it directly.</li>
        </ul>
        <p>
          In development the same code runs locally with Postgres and Redis in Docker, and a Celery worker takes the
          place of Cloud Tasks.
        </p>
      </Section>

      <Section id="limits" title="Limits">
        <ul className="list-disc space-y-3 pl-5">
          <li>Recordings longer than four hours are not supported. They would need to be split into pieces first.</li>
          <li>
            This public demo accepts files up to 200 MB and five uploads per browser in 24 hours, so that it cannot be
            run up by one visitor.
          </li>
          <li>
            Summaries are written by a model from the transcript. They are instructed not to add anything, but they can
            still be wrong, which is why the transcript is always shown next to them.
          </li>
          <li>Transcripts are automatic, so names, numbers and heavily accented speech can be misheard.</li>
          <li>An upload stops if you close the tab, and cannot be resumed. Start it again.</li>
          <li>
            The recording itself is gone after two hours, so it cannot be played or processed again after that. Transcripts and
            summaries cannot be deleted or renamed yet.
          </li>
          <li>The speech service allows about one call a second, so many recordings at once wait their turn.</li>
        </ul>
      </Section>

      <Section id="scale" title="What would change with real users">
        <ul className="list-disc space-y-3 pl-5">
          <li>Accounts and proper authorization in place of the browser id, plus per-user limits.</li>
          <li>Resumable, multi-part uploads, so a dropped connection does not restart a large file.</li>
          <li>Webhooks from the speech service instead of checking back on a timer.</li>
          <li>Splitting very long audio, and cleaning up old files automatically.</li>
          <li>Scanning uploads, and dashboards and alerts on failures and queue length.</li>
        </ul>
      </Section>
    </article>
  );
}
