import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "./api";
import { startPolling, type PollerOptions } from "./poller";

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

const tick = (ms: number) => vi.advanceTimersByTimeAsync(ms);
const down = () => new ApiError(0, "NETWORK", "down");

type Load = ReturnType<typeof vi.fn<() => Promise<string>>>;

/** Starts a poller whose first check happens at once, so `script` sets up what `load` answers before that. */
function setup(script: (load: Load) => void = () => {}, overrides: Partial<PollerOptions<string>> = {}) {
  const log = { data: [] as string[], errors: [] as Array<[number, number]> }; // errors: [status, failures in a row]
  const load: Load = vi.fn<() => Promise<string>>().mockResolvedValue("working");
  script(load);
  const poller = startPolling<string>({
    load,
    delayFor: (data) => (data === "done" ? null : 1000),
    onData: (data) => log.data.push(data),
    onError: (error, failures) => log.errors.push([error.status, failures]),
    ...overrides,
  });
  return { load, log, poller };
}

describe("asking again", () => {
  it("asks at once, then again after each delay, and stops when told the answer is final", async () => {
    const { load, log } = setup((l) => l.mockResolvedValueOnce("working").mockResolvedValueOnce("working").mockResolvedValue("done"));
    await tick(0);
    expect(log.data).toEqual(["working"]);
    await tick(1000);
    await tick(1000);
    expect(log.data).toEqual(["working", "working", "done"]);
    await tick(60_000);
    expect(load).toHaveBeenCalledTimes(3); // "done" asked for no further check
  });

  it("refresh() asks once more even after the loop has stopped", async () => {
    const { load, poller } = setup((l) => l.mockResolvedValue("done"));
    await tick(0);
    poller.refresh();
    await tick(0);
    expect(load).toHaveBeenCalledTimes(2);
  });

  it("never runs two checks at once", async () => {
    let release: (value: string) => void = () => {};
    const { load, poller } = setup((l) => l.mockReturnValue(new Promise<string>((resolve) => (release = resolve))));
    poller.refresh();
    poller.refresh();
    await tick(0);
    expect(load).toHaveBeenCalledTimes(1); // the first is still waiting; neither refresh started another
    release("done");
  });
});

describe("when asking fails", () => {
  it("backs off, doubling up to 30 seconds", async () => {
    const { load, log } = setup((l) => l.mockRejectedValue(down()));
    await tick(0);
    expect(log.errors).toEqual([[0, 1]]);
    await tick(4999);
    expect(load).toHaveBeenCalledTimes(1); // the first wait is 5 s
    await tick(1);
    expect(load).toHaveBeenCalledTimes(2);
    await tick(10_000);
    expect(load).toHaveBeenCalledTimes(3); // then 10 s
    expect(log.errors.at(-1)).toEqual([0, 3]);
    await tick(20_000);
    await tick(30_000);
    await tick(30_000);
    expect(load).toHaveBeenCalledTimes(6); // then 20 s, then 30 s, and never longer than 30 s
  });

  it("counts failures in a row, and starts over after a good answer", async () => {
    const { load, log } = setup((l) =>
      l.mockRejectedValueOnce(down()).mockRejectedValueOnce(down()).mockResolvedValue("working"),
    );
    await tick(0);
    await tick(5000);
    expect(log.errors).toEqual([
      [0, 1],
      [0, 2],
    ]);
    await tick(10_000); // the third check succeeds
    expect(log.data).toEqual(["working"]);
    load.mockRejectedValueOnce(down());
    await tick(1000);
    expect(log.errors.at(-1)).toEqual([0, 1]); // counting started over
  });

  it.each([401, 404])("stops for good on a %i: asking again cannot change it", async (status) => {
    const { load, log } = setup((l) => l.mockRejectedValue(new ApiError(status, "X", "no")));
    await tick(0);
    await tick(120_000);
    expect(load).toHaveBeenCalledTimes(1);
    expect(log.errors).toEqual([[status, 1]]);
  });

  it("reports something that is not an ApiError as a generic failure", async () => {
    const { log } = setup((l) => l.mockRejectedValue(new TypeError("boom")));
    await tick(0);
    expect(log.errors).toEqual([[0, 1]]);
  });
});

describe("a tab nobody is looking at", () => {
  it("waits while hidden and asks at once when it comes back", async () => {
    let hidden = false;
    let resume: () => void = () => {};
    const { load } = setup(undefined, {
      isHidden: () => hidden,
      onResume: (callback) => {
        resume = callback;
        return () => {};
      },
    });
    await tick(0);
    expect(load).toHaveBeenCalledTimes(1);
    hidden = true;
    await tick(10_000);
    expect(load).toHaveBeenCalledTimes(1); // the timer fired while hidden and asked nothing
    resume();
    await tick(0);
    expect(load).toHaveBeenCalledTimes(1); // coming back to a tab that is still hidden changes nothing
    hidden = false;
    resume();
    await tick(0);
    expect(load).toHaveBeenCalledTimes(2);
  });
});

describe("stop()", () => {
  it("ends the loop, ignores an answer that arrives afterwards, and unsubscribes", async () => {
    let release: (value: string) => void = () => {};
    const unsubscribe = vi.fn();
    const { load, log, poller } = setup((l) => l.mockReturnValue(new Promise<string>((resolve) => (release = resolve))), {
      onResume: () => unsubscribe,
    });
    poller.stop();
    release("working");
    await tick(60_000);
    expect(log.data).toEqual([]);
    expect(load).toHaveBeenCalledTimes(1);
    expect(unsubscribe).toHaveBeenCalledTimes(1);
  });
});
