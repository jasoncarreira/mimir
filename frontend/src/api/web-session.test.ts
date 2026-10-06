// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest";
import { apiFetchJson, createWebSession, deleteWebSession, restoreWebSession } from "./http";

afterEach(() => { window.localStorage.clear(); vi.restoreAllMocks(); });

it("migrates a legacy key once and deletes it even when exchange fails", async () => {
  window.localStorage.setItem("mimir.api_key", "old-secret");
  const calls: Array<[string, RequestInit]> = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit) => {
    calls.push([url, init]);
    return new Response('{"ok":true}', { headers: { "content-type": "application/json" } });
  }));
  expect(await restoreWebSession()).toBe(true);
  expect(window.localStorage.getItem("mimir.api_key")).toBeNull();
  expect(calls[0][0]).toBe("/api/v1/web/session");
  expect(calls[0][1].method).toBe("POST");
  expect(new Headers(calls[0][1].headers).get("X-API-Key")).toBe("old-secret");
  await apiFetchJson("/api/v1/turns");
  expect(new Headers(calls[1][1].headers).has("X-API-Key")).toBe(false);
  await deleteWebSession();
  expect(calls[2][1].method).toBe("DELETE");
  await createWebSession("new-secret");
  expect(window.localStorage.getItem("mimir.api_key")).toBeNull();
  vi.unstubAllGlobals();
});

it("removes a rejected legacy key without treating it as signed in", async () => {
  window.localStorage.setItem("mimir.api_key", "revoked");
  vi.stubGlobal("fetch", vi.fn(async () => new Response('{"error":"unauthorized"}', {
    status: 401, headers: { "content-type": "application/json" }
  })));
  expect(await restoreWebSession()).toBe(false);
  expect(window.localStorage.getItem("mimir.api_key")).toBeNull();
  vi.unstubAllGlobals();
});
