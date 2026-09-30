import { RecordingList } from "@/components/recording-list";
import { UploadPanel } from "@/components/upload-panel";

export default function Home() {
  return (
    <div className="space-y-16">
      <section aria-labelledby="upload-heading">
        <h1 id="upload-heading" className="font-serif text-3xl font-semibold tracking-tight sm:text-4xl">
          Upload a recording
        </h1>
        <p className="mb-7 mt-3 max-w-prose text-soft">
          You get back a transcript and a short summary. Once the file is uploaded nothing has to stay open: come
          back to the recording whenever you like.
        </p>
        <UploadPanel />
      </section>
      <RecordingList />
    </div>
  );
}
