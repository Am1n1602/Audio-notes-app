// Mirrors the backend's response models (backend/app/schemas). Keep in sync by hand: a handful of small types are not
// worth code generation, and src/lib/api.ts is the only place that turns a response into one of these.

export type JobStatus =
  | "UPLOADING"
  | "UPLOADED"
  | "QUEUED"
  | "TRANSCRIBING"
  | "SUMMARIZING"
  | "COMPLETED"
  | "FAILED";

export interface Summary {
  overview: string;
  key_points: string[];
  action_items: string[];
  decisions: string[];
  uncertainties: string[];
}

/** One row of the history list. No transcript or summary text. */
export interface UploadListItem {
  id: string;
  original_filename: string;
  size_bytes: number;
  duration_seconds: number | null;
  language_code: string;
  status: JobStatus;
  /** The backend's own words for what is happening now, e.g. "Transcribing audio…". Null when nothing is running. */
  progress_message: string | null;
  error_code: string | null;
  /** Safe to show to a person. */
  error_message: string | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  /** Whether the backend will accept a Retry for this job. */
  can_retry: boolean;
}

export interface UploadDetail extends UploadListItem {
  mime_type: string;
  transcript: string | null;
  summary: Summary | null;
}

export interface UploadTarget {
  url: string;
  method: "PUT";
  /** Must be sent exactly as given: they are part of the signature. */
  headers: Record<string, string>;
  expires_in_seconds: number;
}

export interface InitiateResponse {
  id: string;
  status: JobStatus;
  upload: UploadTarget;
}

export interface AudioLink {
  url: string;
  expires_in_seconds: number;
}

export interface Language {
  code: string;
  name: string;
}

/** The limits the backend actually enforces (GET /api/config). */
export interface AppConfig {
  max_upload_bytes: number;
  upload_url_expires_seconds: number;
  audio_extensions: string[];
  languages: Language[];
  max_languages: number;
  default_language_code: string;
}
