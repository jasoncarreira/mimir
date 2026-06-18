import { apiFetchEnvelope, type ApiClientOptions } from "./http";
import type {
  AdminConfigData,
  ApiSuccessEnvelope
} from "./generated/contracts";

export type AdminConfigResponse = AdminConfigData;

export function getAdminConfig(
  options?: ApiClientOptions & RequestInit
): Promise<ApiSuccessEnvelope<AdminConfigData>> {
  return apiFetchEnvelope<AdminConfigData>("/api/v1/admin/config", {
    cache: "no-store",
    ...options
  });
}
