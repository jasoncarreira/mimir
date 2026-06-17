export type QueryValue = string | number | boolean | null | undefined;

export interface RequestJsonOptions extends RequestInit {
  apiKey?: string;
  query?: Record<string, QueryValue | QueryValue[]>;
}

export class ApiError extends Error {
  readonly status: number;
  readonly payload: unknown;

  constructor(message: string, status: number, payload: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.payload = payload;
  }
}

export function buildUrl(path: string, query?: RequestJsonOptions["query"]): string {
  const url = new URL(path, window.location.origin);
  if (!query) return url.pathname + url.search;

  for (const [key, raw] of Object.entries(query)) {
    const values = Array.isArray(raw) ? raw : [raw];
    for (const value of values) {
      if (value === null || value === undefined || value === "") continue;
      url.searchParams.append(key, String(value));
    }
  }
  return url.pathname + url.search;
}

export async function requestJson<T>(
  path: string,
  { apiKey, headers, query, ...init }: RequestJsonOptions = {}
): Promise<T> {
  const mergedHeaders = new Headers(headers);
  if (apiKey) mergedHeaders.set("X-API-Key", apiKey);
  if (init.body && !mergedHeaders.has("Content-Type")) {
    mergedHeaders.set("Content-Type", "application/json");
  }

  const response = await fetch(buildUrl(path, query), {
    ...init,
    headers: mergedHeaders
  });

  let payload: unknown = null;
  const text = await response.text();
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = text;
    }
  }

  if (!response.ok) {
    const detail =
      payload && typeof payload === "object" && "error" in payload
        ? String((payload as { error: unknown }).error)
        : response.statusText;
    throw new ApiError(detail || `HTTP ${response.status}`, response.status, payload);
  }

  return payload as T;
}
