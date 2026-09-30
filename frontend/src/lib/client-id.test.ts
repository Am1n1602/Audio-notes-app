import { describe, expect, it } from "vitest";
import { getClientId, newClientId } from "./client-id";

const V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

function memoryStorage(initial: Record<string, string> = {}) {
  const data = { ...initial };
  return {
    data,
    getItem: (key: string) => data[key] ?? null,
    setItem: (key: string, value: string) => void (data[key] = value),
  };
}

describe("newClientId", () => {
  it("is a UUID v4, which is what the backend accepts", () => {
    expect(newClientId()).toMatch(V4);
  });

  it("still makes a valid v4 where crypto.randomUUID does not exist (a plain-http address)", () => {
    const withoutRandomUuid = { getRandomValues: globalThis.crypto.getRandomValues.bind(globalThis.crypto) };
    const ids = new Set(Array.from({ length: 50 }, () => newClientId(withoutRandomUuid as unknown as Crypto)));
    expect([...ids].every((id) => V4.test(id))).toBe(true);
    expect(ids.size).toBe(50); // random, not constant
  });
});

describe("getClientId", () => {
  it("makes one the first time, keeps it, and gives the same one afterwards", () => {
    const storage = memoryStorage();
    const first = getClientId(storage);
    expect(first.persisted).toBe(true);
    expect(getClientId(storage).id).toBe(first.id);
    expect(Object.values(storage.data)).toEqual([first.id]);
  });

  it("replaces a stored value that is not a v4 UUID (the backend would refuse it)", () => {
    const storage = memoryStorage({ "audio-notes.client-id": "not-a-uuid" });
    expect(getClientId(storage).id).toMatch(V4);
  });

  it("keeps working, for this page only, when the browser will not store anything", () => {
    const blocked = {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("blocked");
      },
    };
    const first = getClientId(blocked);
    expect(first.persisted).toBe(false);
    expect(first.id).toMatch(V4);
    expect(getClientId(blocked).id).toBe(first.id); // one id per page load, so requests stay consistent
  });
});
