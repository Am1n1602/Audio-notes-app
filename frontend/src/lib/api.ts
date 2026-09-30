import { getClientId } from "./client-id";
import type {
  AppConfig,
  AudioLink,
  InitiateResponse,
  UploadDetail,
  UploadListItem,
} from "./types";

// The API's address is public on purpose (bundled into browser code): it is only an address, never a secret.
const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/+$/, "");

/** A failed call, in words a person can read. `code` is the backend's stable code, or NETWORK / UNEXPECTED. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

interface ErrorBody {
  error?: { code?: unknown; message?: unknown };
}

async function errorFrom(res: Response): Promise<ApiError> {
  let body: ErrorBody | null = null;
  try {
    body = (await res.json()) as ErrorBody;
  } catch {
    // not JSON: a proxy or gateway answered instead of the API
  }
  const { code, message } = body?.error ?? {};
  if (typeof code === "string" && typeof message === "string") return new ApiError(res.status, code, message);
  return new ApiError(res.status, "UNEXPECTED", "Something went wrong on our side. Please try again.");
}

async function request<T>(path: string, init: RequestInit = {}, withClientId = true): Promise<T> {
  if (!API_BASE) throw new ApiError(0, "NOT_CONFIGURED", "The app is not connected to its server.");
  const headers = new Headers(init.headers);
  if (withClientId) headers.set("X-Client-Id", getClientId().id);
  if (init.body) headers.set("Content-Type", "application/json");

  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, { ...init, headers, cache: "no-store" });
  } catch {
    throw new ApiError(0, "NETWORK", "Can't reach the server. Check your connection and try again.");
  }
  if (!res.ok) throw await errorFrom(res);
  return (await res.json()) as T;
}

export const getConfig = () => request<AppConfig>("/api/config", {}, false);

export const listUploads = () => request<UploadListItem[]>("/api/uploads");

export const getUpload = (id: string) => request<UploadDetail>(`/api/uploads/${encodeURIComponent(id)}`);

export const initiateUpload = (file: { name: string; size: number }, languageCode: string) =>
  request<InitiateResponse>("/api/uploads/initiate", {
    method: "POST",
    body: JSON.stringify({ filename: file.name, size_bytes: file.size, language_code: languageCode }),
  });

export const completeUpload = (id: string) =>
  request<UploadDetail>(`/api/uploads/${encodeURIComponent(id)}/complete`, { method: "POST" });

export const retryUpload = (id: string) =>
  request<UploadDetail>(`/api/uploads/${encodeURIComponent(id)}/retry`, { method: "POST" });

export const getAudioLink = (id: string) => request<AudioLink>(`/api/uploads/${encodeURIComponent(id)}/audio`);
