import type * as ReactType from "react";
import type { PluginHost } from "../../../../frontend/src/dashboardExtensions";

declare global {
  interface Window {
    mimirPluginHost?: PluginHost;
    mimirCultivate?: { readonly PullRequests: typeof import("./prs").PullRequests };
  }
}

export const host = window.mimirPluginHost!;
export const UI = host.uiKit.components;
export type BadgeTone = ReactType.ComponentProps<typeof UI.Badge>["tone"];

const base = "/api/v1/plugins/cultivate";

export async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await host.authenticatedFetch(`${base}${path}`, options);
  const body = await response.json();
  if (!response.ok || !body.ok) throw new Error(body.error?.message || `Request failed (${response.status})`);
  return body.data as T;
}

export function safeLink(value: string | null): string | null {
  if (!value) return null;
  try {
    const url = new URL(value, window.location.origin);
    return url.protocol === "https:" || (url.protocol === "http:" && url.origin === window.location.origin)
      ? url.href : null;
  } catch { return null; }
}
