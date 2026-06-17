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

function getStoredApiKey(): string {
  try {
    return globalThis.localStorage?.getItem(MIMIR_API_KEY_STORAGE_KEY) ?? "";
  } catch {
    return "";
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
  const key = apiKey ?? getStoredApiKey();
  if (key) mergedHeaders.set("X-API-Key", key);

  const response = await fetchImpl(`${baseUrl}${path}`, {
    ...request,
    headers: mergedHeaders
  });

  const contentType = response.headers.get("content-type") ?? "";
  const body = contentType.includes("application/json")
    ? await response.json()
    : await response.text();

  if (!response.ok) {
    const message =
      body && typeof body === "object" && "error" in body
        ? String((body as { error?: unknown }).error)
        : undefined;
    throw new ApiError(response.status, body, message);
  }
  return body as T;
}
