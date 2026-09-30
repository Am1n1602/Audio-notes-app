// Per-browser history. There are no accounts: each browser makes up a random id once, keeps it, and sends it with every
// call (the backend stores only its hash). See backend/app/api/owner.py for what that does and does not protect.

const KEY = "audio-notes.client-id";
const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

let remembered: string | null = null;

/** A random UUID v4, which is what the backend requires (122 bits from the browser's secure random source). */
export function newClientId(cryptoApi: Crypto = globalThis.crypto): string {
  if (typeof cryptoApi.randomUUID === "function") return cryptoApi.randomUUID();
  // randomUUID only exists in secure contexts (https or localhost); a plain-http address still has getRandomValues.
  const bytes = cryptoApi.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export interface ClientId {
  id: string;
  /** False when this browser would not let us keep it (private mode, blocked storage): history is lost on reload. */
  persisted: boolean;
}

export function getClientId(storage?: Pick<Storage, "getItem" | "setItem">): ClientId {
  try {
    const store = storage ?? globalThis.localStorage;
    const saved = store.getItem(KEY);
    if (saved && UUID_V4.test(saved)) return { id: saved, persisted: true };
    const id = newClientId();
    store.setItem(KEY, id);
    return { id, persisted: true };
  } catch {
    // Storage is unavailable. Keep one id for this page's lifetime so the session still works.
    remembered ??= newClientId();
    return { id: remembered, persisted: false };
  }
}
