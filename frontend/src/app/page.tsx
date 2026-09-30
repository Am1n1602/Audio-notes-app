import ApiStatus from "@/components/ApiStatus";

export default function Home() {
  return (
    <main className="mx-auto flex w-full max-w-2xl flex-1 flex-col gap-6 px-6 py-16">
      <h1 className="text-3xl font-semibold">Audio Notes</h1>
      <p className="text-neutral-600 dark:text-neutral-400">
        Upload a recording and get a transcript and summary. The upload flow arrives in a later phase;
        for now this page only checks that the services are connected.
      </p>
      <ApiStatus />
    </main>
  );
}
