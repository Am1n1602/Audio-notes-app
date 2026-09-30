import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

// Logic tests only (formatting, status rules, the API client, the upload state machine). Screens are checked in a real
// browser against the real backend.
export default defineConfig({
  test: { environment: "node", include: ["src/**/*.test.ts"] },
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
});
