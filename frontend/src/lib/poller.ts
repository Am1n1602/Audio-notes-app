import { ApiError } from "./api";
import { backoffDelay } from "./polling";

// The loop behind usePolled, kept free of React and the DOM so it can be tested with fake timers.

// These answers will not change by asking again, so asking stops.
const FINAL_ERRORS = new Set([401, 404]);

export interface PollerOptions<T> {
  load: () => Promise<T>;
  /** Milliseconds until the next check, or null to stop asking (refresh() still asks once). */
  delayFor: (data: T) => number | null;
  onData: (data: T) => void;
  /** `failures` counts checks that failed in a row. */
  onError: (error: ApiError, failures: number) => void;
  /** True while nobody is looking (a hidden tab): the loop waits instead of asking. */
  isHidden?: () => boolean;
  /** Subscribe `resume` to "somebody is looking again / the network is back"; returns the unsubscribe. */
  onResume?: (resume: () => void) => () => void;
}

/**
 * Asks `load` now and then again after each `delayFor` until it says stop. Waits while hidden and asks at once on resume,
 * backs off (2x, up to 30 s) after a failure, stops on an answer asking again cannot change, and never has two checks
 * running at once.
 */
export function startPolling<T>({
  load,
  delayFor,
  onData,
  onError,
  isHidden = () => false,
  onResume = () => () => {},
}: PollerOptions<T>) {
  let stopped = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let inFlight = false;
  let failures = 0;

  const schedule = (ms: number) => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      if (!isHidden()) void run(); // hidden: wait, and resume() asks as soon as the tab is back
    }, ms);
  };

  async function run(): Promise<void> {
    if (inFlight || stopped) return;
    inFlight = true;
    clearTimeout(timer);
    try {
      const data = await load();
      if (stopped) return;
      failures = 0;
      onData(data);
      const delay = delayFor(data);
      if (delay !== null) schedule(delay);
    } catch (caught) {
      if (stopped) return;
      failures += 1;
      const error = caught instanceof ApiError ? caught : new ApiError(0, "UNEXPECTED", "Something went wrong.");
      onError(error, failures);
      if (!FINAL_ERRORS.has(error.status)) schedule(backoffDelay(2500, failures));
    } finally {
      inFlight = false;
    }
  }

  const unsubscribe = onResume(() => {
    if (!isHidden()) void run();
  });
  void run();

  return {
    refresh: () => void run(),
    stop() {
      stopped = true;
      clearTimeout(timer);
      unsubscribe();
    },
  };
}
