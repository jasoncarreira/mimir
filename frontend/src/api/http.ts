export type ApiKeyProvider = string | (() => string | null | undefined);

export interface ApiClientOptions {
  baseUrl?: string;
  apiKey?: ApiKeyProvider;
  fetchImpl?: typeof fetch;
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

function resolveApiKey(apiKey: ApiKeyProvider | undefined): string | undefined {
  if (typeof apiKey === "function") {
    return apiKey() || undefined;
  }
  return apiKey || undefined;
}

export function createApiClient(options: ApiClientOptions = {}) {
  const fetchImpl = options.fetchImpl ?? fetch;
  const baseUrl = options.baseUrl ?? "";

  async function requestJson<T>(
    path: string,
    init: RequestInit = {}
  ): Promise<T> {
    const headers = new Headers(init.headers);
    const key = resolveApiKey(options.apiKey);
    if (key && !headers.has("X-API-Key")) {
      headers.set("X-API-Key", key);
    }
    if (init.body && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }

    const response = await fetchImpl(baseUrl + path, { ...init, headers });
    const text = await response.text();
    const payload = text ? JSON.parse(text) : null;
    if (!response.ok) {
      const message =
        payload && typeof payload === "object" && "error" in payload
          ? String((payload as { error: unknown }).error)
          : `HTTP ${response.status}`;
      throw new ApiError(message, response.status, payload);
    }
    return payload as T;
  }

  return { requestJson };
}

export type ApiClient = ReturnType<typeof createApiClient>;

export function withQuery(
  path: string,
  params: Record<string, string | number | boolean | null | undefined>
): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined && value !== "") {
      search.set(key, String(value));
    }
  }
  const qs = search.toString();
  return qs ? `${path}?${qs}` : path;
}
