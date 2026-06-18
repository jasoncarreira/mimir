import type { AdminConfigData } from "./generated/contracts";
import { apiFetchEnvelope } from "./http";

export async function fetchAdminConfig(options: RequestInit = {}) {
  return apiFetchEnvelope<AdminConfigData>("/api/v1/admin/config", {
    cache: "no-store",
    ...options
  });
}
