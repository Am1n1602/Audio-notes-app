import { STAGES, type StageState } from "@/lib/status";

const bar: Record<StageState, string> = {
  done: "bg-ink",
  active: "bg-signal ruler-running",
  waiting: "border border-signal bg-transparent",
  pending: "bg-rule",
  failed: "bg-bad",
};

const spoken: Record<StageState, string> = {
  done: "done",
  active: "in progress",
  waiting: "waiting to start",
  pending: "not started",
  failed: "failed",
};

/**
 * The job's four stages as a ruler. Each segment's state comes from the backend's status, nothing else: a segment
 * only fills when that stage is finished, and the running one sweeps because its length is not known, which is the
 * honest way to show work whose progress the speech service does not report.
 */
export function Ruler({ states }: { states: StageState[] }) {
  return (
    <ol className="grid grid-cols-4 gap-1.5">
      {STAGES.map((stage, index) => {
        const state = states[index];
        return (
          <li key={stage.key} aria-current={state === "active" ? "step" : undefined}>
            <div className={`h-1.5 rounded-control ${bar[state]}`} />
            <p className={`mt-2 text-sm ${state === "pending" ? "text-soft" : "font-medium"}`}>
              {stage.label}
              <span className="sr-only">, {spoken[state]}</span>
            </p>
          </li>
        );
      })}
    </ol>
  );
}

/** Work with no known length: a bar that sweeps. Used where the backend has not said how far along it is. */
export function WorkingBar({ label }: { label: string }) {
  return <div role="progressbar" aria-label={label} className="h-1.5 rounded-control bg-signal ruler-running" />;
}

/** The one determinate bar: bytes that have left the browser, out of the file's size. */
export function UploadBar({ loaded, total }: { loaded: number; total: number }) {
  const percent = total > 0 ? Math.min(100, Math.floor((loaded / total) * 100)) : 0;
  return (
    <div
      role="progressbar"
      aria-label="Upload progress"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={percent}
      className="h-1.5 overflow-hidden rounded-control bg-rule"
    >
      <div className="h-full bg-ink transition-[width] duration-150" style={{ width: `${percent}%` }} />
    </div>
  );
}
