import { describe, expect, it } from "vitest";
import { noteServerTime, serverNow } from "./clock";

describe("the server's clock", () => {
  it("is the browser's own until a response says otherwise, then follows the server", () => {
    const received = 1_000_000_000_000;
    noteServerTime(new Date(received + 180_000).toUTCString(), received); // the server is three minutes ahead
    expect(serverNow() - Date.now()).toBeGreaterThan(179_000);
    expect(serverNow() - Date.now()).toBeLessThan(181_000);
  });

  it("ignores a missing or unreadable Date header instead of corrupting the clock", () => {
    const before = serverNow() - Date.now();
    noteServerTime(null);
    noteServerTime("not a date");
    expect(Math.abs(serverNow() - Date.now() - before)).toBeLessThan(5); // unchanged (allowing for the ms between reads)
  });
});
