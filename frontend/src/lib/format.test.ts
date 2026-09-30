import { describe, expect, it } from "vitest";
import { formatAgo, formatBytes, formatDuration, languageLabel, languageTag, wordCount } from "./format";

describe("formatBytes", () => {
  it.each([
    [0, "0\u00a0B"],
    [900, "900\u00a0B"],
    [2048, "2\u00a0KB"],
    [5_226_310, "5.0\u00a0MB"],
    [2 * 1024 ** 3, "2.0\u00a0GB"],
  ])("%d bytes is %s", (bytes, text) => expect(formatBytes(bytes)).toBe(text)); // a non-breaking space keeps number and unit together
});

describe("formatDuration", () => {
  it.each([
    [6.4, "6 s"],
    [60, "1 min"],
    [118.51, "1 min 59 s"],
    [3600, "1 h"],
    [7440, "2 h 4 min"],
  ])("%d seconds is %s", (seconds, text) => expect(formatDuration(seconds)).toBe(text));
});

describe("formatAgo", () => {
  const now = Date.parse("2026-09-30T12:00:00Z");
  const ago = (seconds: number) => formatAgo(new Date(now - seconds * 1000).toISOString(), now);
  it.each([
    [1, "just now"],
    [42, "42 s ago"],
    [180, "3 min ago"],
    [7200, "2 h ago"],
    [86_400 * 1.2, "yesterday"],
    [86_400 * 5, "5 days ago"],
  ])("%d seconds ago reads %s", (seconds, text) => expect(ago(seconds)).toBe(text));

  it("never reports a time in the future as negative (clock skew)", () => {
    expect(formatAgo(new Date(now + 30_000).toISOString(), now)).toBe("just now");
  });
});

describe("languages", () => {
  const languages = [
    { code: "hi-IN", name: "Hindi" },
    { code: "en-IN", name: "English (India)" },
  ];
  it("names each code, and keeps one the backend did not list", () => {
    expect(languageLabel("hi-IN,en-IN", languages)).toBe("Hindi, English (India)");
    expect(languageLabel("xx-YY", languages)).toBe("xx-YY");
  });
  it("gives a language tag only when there is exactly one language", () => {
    expect(languageTag("hi-IN")).toBe("hi-IN");
    expect(languageTag("hi-IN,en-IN")).toBeUndefined();
  });
});

describe("wordCount", () => {
  it("counts words in any script and ignores extra whitespace", () => {
    expect(wordCount("  good   morning everyone ")).toBe(3);
    expect(wordCount("सुप्रभात सभी को")).toBe(3);
    expect(wordCount("   ")).toBe(0);
  });
});
