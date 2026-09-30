"use client";

import { useSyncExternalStore } from "react";
import { uploadStore, type UploadState } from "@/lib/upload-store";

const IDLE: UploadState = { phase: "idle" };

/** The upload running in this tab, from anywhere in the app. */
export function useUploadState(): UploadState {
  return useSyncExternalStore(uploadStore.subscribe, uploadStore.getState, () => IDLE);
}
