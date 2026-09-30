import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import type { NextConfig } from "next";

// One .env at the repo root serves the API, the worker and the frontend, but Next only reads .env
// files inside frontend/. Copy over just the NEXT_PUBLIC_* lines (values meant for browser code).
// Deliberately NOT loading the whole file: it holds the Gnani and AWS secrets, which the frontend
// never needs, and anything in process.env during a build can end up in Turbopack's on-disk cache.
// Real environment variables win (Vercel sets NEXT_PUBLIC_API_BASE_URL in its dashboard instead).
const rootEnv = path.resolve(process.cwd(), "..", ".env");
if (existsSync(rootEnv)) {
  for (const line of readFileSync(rootEnv, "utf8").split(/\r?\n/)) {
    const match = line.match(/^(NEXT_PUBLIC_\w+)=(.*)$/);
    if (match && process.env[match[1]] === undefined) {
      process.env[match[1]] = match[2].trim().replace(/^(["'])(.*)\1$/, "$2");
    }
  }
}

const nextConfig: NextConfig = {};

export default nextConfig;
