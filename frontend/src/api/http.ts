import type { ApiEnvelope, ApiSuccessEnvelope } from "./generated/contracts";

export const MIMIR_API_KEY_STORAGE_KEY = "mimir.api_key";

export type QueryValue = string | number | boolean | null | undefined;

export interface ApiClientOptions {
  baseUrl?: string;
  apiKey?: string;
  fetchImpl?: typeof fetch;
}

export class ApiError extends Error {
  status: number;
  body: unknown;

  constructor(status: number, body: unknown, message?: string) {
    super(message ?? `HTTP ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

export async function createWebSession(key: string): Promise<void> {
  await apiFetchJson("/api/v1/web/session", { method: "POST", apiKey: key });
}

export async function deleteWebSession(): Promise<void> {
  await apiFetchJson("/api/v1/web/session", { method: "DELETE" });
}

export async function restoreWebSession(): Promise<boolean> {
  let legacy = "";
  try {
    legacy = globalThis.localStorage?.getItem(MIMIR_API_KEY_STORAGE_KEY) ?? "";
    globalThis.localStorage?.removeItem(MIMIR_API_KEY_STORAGE_KEY);
  } catch {
    // Storage may be disabled; the cookie can still be checked.
  }
  try {
    if (legacy) await createWebSession(legacy);
    else await apiFetchJson("/api/v1/web/session");
    return true;
  } catch {
    return false;
  }
}

export function buildQuery(params: Record<string, QueryValue>): string {
  const q = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined || value === "") continue;
    q.set(key, String(value));
  }
  const text = q.toString();
  return text ? `?${text}` : "";
}

export async function apiFetchJson<T>(
  path: string,
  options: RequestInit & ApiClientOptions = {}
): Promise<T> {
  const {
    baseUrl = "",
    apiKey,
    fetchImpl = fetch,
    headers,
    ...request
  } = options;
  const mergedHeaders = new Headers(headers);
  if (apiKey) mergedHeaders.set("X-API-Key", apiKey);

  const response = await fetchImpl(`${baseUrl}${path}`, {
    ...request,
    headers: mergedHeaders
  });

  const contentType = response.headers.get("content-type") ?? "";
  const body = contentType.includes("application/json")
    ? await response.json()
    : await response.text();

  if (!response.ok) {
    let message: string | undefined;
    if (body && typeof body === "object" && "error" in body) {
      const error = (body as { error?: unknown }).error;
      message =
        error && typeof error === "object" && "message" in error
          ? String((error as { message?: unknown }).message)
          : String(error);
    }
    throw new ApiError(response.status, body, message);
  }
  return body as T;
}

export async function apiFetchEnvelope<TData, TMeta = undefined>(
  path: string,
  options: RequestInit & ApiClientOptions = {}
): Promise<ApiSuccessEnvelope<TData, TMeta>> {
  const envelope = await apiFetchJson<ApiEnvelope<TData, TMeta>>(path, options);
  if (!envelope.ok) {
    throw new ApiError(200, envelope, envelope.error.message);
  }
  return envelope;
}
