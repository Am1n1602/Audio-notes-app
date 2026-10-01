import { formatBytes } from "./format";
import type { AppConfig } from "./types";

/** "WAV, MP3 or AMR": the file types the backend accepts, as a person would list them. */
export function formatTypes(config: Pick<AppConfig, "audio_extensions">): string {
  const names = config.audio_extensions.map((extension) => extension.slice(1).toUpperCase());
  return names.length > 1 ? `${names.slice(0, -1).join(", ")} or ${names.at(-1)}` : (names[0] ?? "");
}

/**
 * What is wrong with this file, in words, or null if nothing is. It repeats the backend's rules so a person hears about
 * a problem before a large file starts uploading; the backend still checks every upload itself. Until the limits have
 * arrived only the one rule that never changes (not empty) is applied.
 */
export function problemWith(file: Pick<File, "name" | "size">, config: AppConfig | null): string | null {
  if (file.size === 0) return "That file is empty.";
  if (!config) return null;
  // The name as the backend reads it: control characters dropped and the ends trimmed (upload_rules.clean_filename).
  const name = file.name.replace(/[\u0000-\u001f\u007f]/g, "").trim();
  const dot = name.lastIndexOf(".");
  const extension = dot > 0 ? name.slice(dot).toLowerCase() : "";
  if (!config.audio_extensions.includes(extension)) {
    return `${extension ? extension.slice(1).toUpperCase() : "That kind of"} files aren't supported. Use ${formatTypes(config)}.`;
  }
  if (file.size > config.max_upload_bytes) {
    return `That file is ${formatBytes(file.size)}. The limit is ${formatBytes(config.max_upload_bytes)}.`;
  }
  return null;
}
