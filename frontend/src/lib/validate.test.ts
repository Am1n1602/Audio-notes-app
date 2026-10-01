import { describe, expect, it } from "vitest";
import type { AppConfig } from "./types";
import { formatTypes, problemWith } from "./validate";

const config: AppConfig = {
  max_upload_bytes: 2 * 1024 ** 3,
  upload_url_expires_seconds: 900,
  audio_extensions: [".wav", ".mp3", ".amr"],
  languages: [],
  max_languages: 3,
  default_language_code: "en-IN",
};

describe("formatTypes", () => {
  it("lists what the backend accepts the way a person would", () => {
    expect(formatTypes(config)).toBe("WAV, MP3 or AMR");
    expect(formatTypes({ audio_extensions: [".wav"] })).toBe("WAV");
  });
});

describe("problemWith", () => {
  it("accepts a supported file within the limit, whatever the letter case", () => {
    expect(problemWith({ name: "meeting.wav", size: 5_000_000 }, config)).toBeNull();
    expect(problemWith({ name: "MEETING.MP3", size: 5_000_000 }, config)).toBeNull();
  });

  it("names an unsupported type and lists what to use instead", () => {
    expect(problemWith({ name: "notes.txt", size: 100 }, config)).toBe("TXT files aren't supported. Use WAV, MP3 or AMR.");
  });

  it("copes with a file that has no extension, or only a leading dot", () => {
    expect(problemWith({ name: "recording", size: 100 }, config)).toMatch(/^That kind of files aren't supported/);
    expect(problemWith({ name: ".wav", size: 100 }, config)).toMatch(/^That kind of files aren't supported/);
  });

  it("reads the name the way the backend does: stray spaces and control characters do not hide the extension", () => {
    expect(problemWith({ name: "meeting.wav ", size: 100 }, config)).toBeNull();
    expect(problemWith({ name: " meeting.wav", size: 100 }, config)).toBeNull();
    expect(problemWith({ name: "meeting.w\u0007av", size: 100 }, config)).toBeNull();
  });

  it("uses the last extension of a name with several dots", () => {
    expect(problemWith({ name: "call.2026.09.30.wav", size: 100 }, config)).toBeNull();
  });

  it("refuses a file over the limit and says both sizes", () => {
    expect(problemWith({ name: "long.wav", size: 3 * 1024 ** 3 }, config)).toBe("That file is 3.0 GB. The limit is 2.0 GB.");
  });

  it("accepts a file at exactly the limit", () => {
    expect(problemWith({ name: "edge.wav", size: config.max_upload_bytes }, config)).toBeNull();
  });

  it("refuses an empty file even before the limits have loaded, and leaves the rest to the backend", () => {
    expect(problemWith({ name: "a.wav", size: 0 }, null)).toBe("That file is empty.");
    expect(problemWith({ name: "notes.txt", size: 10 }, null)).toBeNull();
  });
});
