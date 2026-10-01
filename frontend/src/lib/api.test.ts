import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const CLIENT = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

// The API address is read once when the module loads, so each test loads a fresh copy with the address it wants.
async function loadApi(base: string | null = "https://api.example.test/") {
  vi.resetModules();
  vi.stubEnv("NEXT_PUBLIC_API_BASE_URL", base ?? ""); // an empty value is how a missing address looks
  return import("./api");
}

function respond(status: number, body: unknown) {
  return new Response(typeof body === "string" ? body : JSON.stringify(body), { status });
}

let fetchMock: ReturnType<typeof vi.fn>;
beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe("requests", () => {
  it("send the browser's client id, never cache, and join the address without a double slash", async () => {
    const api = await loadApi();
    fetchMock.mockResolvedValue(respond(200, []));
    await api.listUploads();
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("https://api.example.test/api/uploads");
    expect(new Headers(init.headers).get("X-Client-Id")).toMatch(CLIENT);
    expect(init.cache).toBe("no-store");
  });

  it("give every request a time limit, so a connection that never answers cannot stall what waits on it", async () => {
    const api = await loadApi();
    fetchMock.mockResolvedValue(respond(200, []));
    await api.listUploads();
    expect(fetchMock.mock.calls[0][1].signal).toBeInstanceOf(AbortSignal);
  });

  it("learn the server's clock from the response's Date header", async () => {
    const api = await loadApi();
    const clock = await import("./clock");
    const serverTime = Date.now() + 5 * 60_000; // the server is five minutes ahead of this browser
    fetchMock.mockResolvedValue(
      new Response("[]", { status: 200, headers: { Date: new Date(serverTime).toUTCString() } }),
    );
    await api.listUploads();
    expect(Math.abs(clock.serverNow() - serverTime)).toBeLessThan(2000); // the header has one-second resolution
  });

  it("post the file's name, size and language as JSON when starting an upload", async () => {
    const api = await loadApi();
    fetchMock.mockResolvedValue(respond(201, { id: "job-1" }));
    await api.initiateUpload({ name: "meeting.wav", size: 5_226_310 }, "hi-IN,en-IN");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("https://api.example.test/api/uploads/initiate");
    expect(init.method).toBe("POST");
    expect(new Headers(init.headers).get("Content-Type")).toBe("application/json");
    expect(JSON.parse(init.body)).toEqual({ filename: "meeting.wav", size_bytes: 5_226_310, language_code: "hi-IN,en-IN" });
  });

  it("do not send a client id for the public config", async () => {
    const api = await loadApi();
    fetchMock.mockResolvedValue(respond(200, {}));
    await api.getConfig();
    expect(new Headers(fetchMock.mock.calls[0][1].headers).has("X-Client-Id")).toBe(false);
  });

  it("escape a job id so it can never change the path", async () => {
    const api = await loadApi();
    fetchMock.mockResolvedValue(respond(200, {}));
    await api.getUpload("a/../b?x=1");
    expect(fetchMock.mock.calls[0][0]).toBe("https://api.example.test/api/uploads/a%2F..%2Fb%3Fx%3D1");
  });
});

describe("failures", () => {
  it("carry the backend's own words and stable code", async () => {
    const api = await loadApi();
    fetchMock.mockResolvedValue(
      respond(409, { error: { code: "UPLOAD_NOT_FOUND", message: "The file has not arrived in storage." } }),
    );
    await expect(api.completeUpload("job-1")).rejects.toMatchObject({
      name: "ApiError",
      status: 409,
      code: "UPLOAD_NOT_FOUND",
      message: "The file has not arrived in storage.",
    });
  });

  it("do not pass on whatever a gateway or proxy answered with", async () => {
    const api = await loadApi();
    fetchMock.mockResolvedValue(respond(502, "<html>Bad gateway: upstream 10.0.0.7:8000 refused</html>"));
    const error = await api.getUpload("job-1").catch((e) => e);
    expect(error).toMatchObject({ status: 502, code: "UNEXPECTED" });
    expect(error.message).toBe("Something went wrong on our side. Please try again.");
    expect(error.message).not.toMatch(/10\.0\.0\.7|html/i);
  });

  it("ignore a JSON body that does not have the error shape", async () => {
    const api = await loadApi();
    fetchMock.mockResolvedValue(respond(500, { detail: "Traceback (most recent call last): ..." }));
    const error = await api.getUpload("job-1").catch((e) => e);
    expect(error.code).toBe("UNEXPECTED");
    expect(error.message).not.toMatch(/Traceback/);
  });

  it("say plainly that the server can't be reached", async () => {
    const api = await loadApi();
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
    await expect(api.listUploads()).rejects.toMatchObject({ status: 0, code: "NETWORK" });
  });

  it("explain a missing API address instead of calling a wrong place", async () => {
    const api = await loadApi(null);
    await expect(api.listUploads()).rejects.toMatchObject({ code: "NOT_CONFIGURED" });
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
