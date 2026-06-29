import { apiFetchEnvelope, type ApiClientOptions } from "./http";
import type { ApiSuccessEnvelope, WhoamiData } from "./generated/contracts";
import type { SkinId } from "../skins/types";

// GET /api/v1/whoami — the authenticated caller's identity + roles, so the app
// can adapt (hide admin-only sections for non-admins). github #563.
export function getWhoami(
  options?: ApiClientOptions & RequestInit
): Promise<ApiSuccessEnvelope<WhoamiData>> {
  return apiFetchEnvelope<WhoamiData>("/api/v1/whoami", { cache: "no-store", ...options });
}


export function updateUserPrefs(
  prefs: { skin?: SkinId | null },
  options?: ApiClientOptions & RequestInit
): Promise<ApiSuccessEnvelope<WhoamiData>> {
  return apiFetchEnvelope<WhoamiData>("/api/v1/user/prefs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prefs }),
    ...options
  });
}
