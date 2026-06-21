import { useNavigate, useSearchParams } from "react-router-dom";
import type { DashboardSurface } from "./dashboardExtensions";

export const ROUTE_STATE_KEYS = [
  "tab",
  "turn",
  "session",
  "atom",
  "issue",
  "job",
  "filter",
  "from",
  "to",
  "channel",
  "event",
  "target",
  "q",
  "path"
] as const;

export type RouteStateKey = typeof ROUTE_STATE_KEYS[number];
export type RouteStatePatch = Partial<Record<RouteStateKey, string | number | null | undefined>>;

const SECRET_QUERY_KEY_RE = /(api[_-]?key|token|secret|password|credential|authorization|auth[_-]?key)/i;

const SAFE_RELATIVE_PATH_RE = /^(?!.*\\)(?!.*(?:^|\/)\.\.(?:\/|$))\/(?!\/)/;

export function sanitizeHref(href: string | null | undefined): string | null {
  const value = (href || "").trim();
  if (!value) return null;

  if (SAFE_RELATIVE_PATH_RE.test(value)) return value;

  try {
    const parsed = new URL(value);
    if (parsed.protocol === "http:" || parsed.protocol === "https:") {
      return parsed.href;
    }
  } catch {
    return null;
  }

  return null;
}

export function scrubSecretQueryParams(
  location: Pick<Location, "pathname" | "search" | "hash"> = window.location
): string | null {
  const params = sanitizedSearchParams(location.search);
  const nextSearch = params.toString();
  const currentSearch = location.search.startsWith("?")
    ? location.search.slice(1)
    : location.search;
  if (nextSearch === currentSearch) return null;
  return `${location.pathname}${nextSearch ? `?${nextSearch}` : ""}${location.hash}`;
}

export function sanitizedSearchParams(
  base?: URLSearchParams | string,
  patch: RouteStatePatch = {}
): URLSearchParams {
  const params = new URLSearchParams(base);
  for (const key of Array.from(params.keys())) {
    if (SECRET_QUERY_KEY_RE.test(key)) params.delete(key);
  }
  for (const [key, value] of Object.entries(patch)) {
    if (SECRET_QUERY_KEY_RE.test(key)) continue;
    const text = value == null ? "" : String(value).trim();
    if (text) params.set(key, text);
    else params.delete(key);
  }
  return params;
}

export function drilldownHref(pathname: `/${string}`, patch: RouteStatePatch = {}, base?: URLSearchParams | string): string {
  const params = sanitizedSearchParams(base, patch);
  const search = params.toString();
  return search ? `${pathname}?${search}` : pathname;
}

export function useRouteState(surface: DashboardSurface) {
  const [searchParams, setSearchParams] = useSearchParams();
  const activeTab = searchParams.get("tab") || surface.tabs[0];
  const selectedTurn = searchParams.get("turn") || "";
  const selectedSession = searchParams.get("session") || "";
  const selectedAtom = searchParams.get("atom") || "";
  const selectedIssue = searchParams.get("issue") || "";
  const selectedJob = searchParams.get("job") || "";
  const filter = searchParams.get("filter") || "";
  const from = searchParams.get("from") || "";
  const to = searchParams.get("to") || "";
  const channel = searchParams.get("channel") || "";
  const event = searchParams.get("event") || "";
  const target = searchParams.get("target") || "";
  const navigate = useNavigate();

  function update(next: RouteStatePatch, options: { replace?: boolean } = {}) {
    setSearchParams(sanitizedSearchParams(searchParams, next), { replace: options.replace ?? false });
  }

  function href(pathname: `/${string}`, patch: RouteStatePatch = {}) {
    return drilldownHref(pathname, patch);
  }

  return {
    activeTab,
    selectedTurn,
    selectedSession,
    selectedAtom,
    selectedIssue,
    selectedJob,
    filter,
    from,
    to,
    channel,
    event,
    target,
    update,
    href,
    navigate
  };
}
