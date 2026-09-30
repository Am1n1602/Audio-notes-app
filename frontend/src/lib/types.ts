// Mirrors backend/app/schemas/health.py. Keep in sync by hand (two small types are not worth codegen).
export type CheckStatus = "ok" | "error";

export interface Health {
  status: "ok" | "degraded";
  db: CheckStatus;
  redis: CheckStatus;
}
