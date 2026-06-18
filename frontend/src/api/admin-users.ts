import { apiFetchEnvelope, type ApiClientOptions } from "./http";
import type {
  AdminUsersData,
  ApiSuccessEnvelope,
  IssueKeyData,
  RevokeKeyData
} from "./generated/contracts";

// Admin Users page clients (github #563). All hit /api/v1/admin/* and are
// admin-gated server-side. listUsers never returns key material; issueUserKey
// returns the raw key ONCE for out-of-band hand-off.

export function listUsers(
  options?: ApiClientOptions & RequestInit
): Promise<ApiSuccessEnvelope<AdminUsersData>> {
  return apiFetchEnvelope<AdminUsersData>("/api/v1/admin/users", {
    cache: "no-store",
    ...options
  });
}

export function issueUserKey(
  canonical: string,
  role: "user" | "admin" | null,
  options?: ApiClientOptions & RequestInit
): Promise<ApiSuccessEnvelope<IssueKeyData>> {
  return apiFetchEnvelope<IssueKeyData>("/api/v1/admin/users/key", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(role ? { canonical, role } : { canonical }),
    ...options
  });
}

export function revokeUserKey(
  canonical: string,
  options?: ApiClientOptions & RequestInit
): Promise<ApiSuccessEnvelope<RevokeKeyData>> {
  return apiFetchEnvelope<RevokeKeyData>("/api/v1/admin/users/revoke", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ canonical }),
    ...options
  });
}
