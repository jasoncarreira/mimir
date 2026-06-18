// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { classifySagaEvidence, SagaDashboard } from "./SagaDashboard";

function envelope(data: unknown, meta?: unknown) {
  return { ok: true, version: "v1", data, meta };
}

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    headers: new Headers({ "content-type": "application/json" }),
    json: async () => body
  } as Response;
}

function renderDashboard() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } }
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <SagaDashboard />
    </QueryClientProvider>
  );
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("SAGA dashboard rendering", () => {
  it("classifies raw atoms, observations, and triple-shaped evidence separately", () => {
    expect(classifySagaEvidence({ id: "a", content_preview: "raw", memory_type: "raw" })).toBe("atom");
    expect(classifySagaEvidence({ id: "o", content_preview: "obs", memory_type: "observation" })).toBe("observation");
    expect(classifySagaEvidence({ subject: "s", predicate: "p", object: "o" })).toBe("triple");
  });

  it("renders representative mixed atom and observation results in separate sections", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (url.includes("view=stats")) {
        return jsonResponse(envelope({
          ready: true,
          atom_count: 2,
          session_count: 1,
          triple_count: 1,
          tombstoned_count: 0,
          schema_version: 6
        }));
      }
      if (url.includes("view=recent")) {
        return jsonResponse(envelope({
          atoms: [
            {
              id: "raw-1",
              content_preview: "Raw turn content",
              memory_type: "raw",
              stream: "episodic",
              encoding_confidence: 0.7,
              created_at: "2026-06-18T00:00:00Z"
            },
            {
              id: "obs-1",
              content_preview: "User prefers concise answers",
              memory_type: "observation",
              stream: "semantic",
              encoding_confidence: 0.95,
              created_at: "2026-06-18T00:01:00Z"
            }
          ],
          channels: []
        }, { cursor: "obs-1", limit: 50, total: 2, truncated: false }));
      }
      return jsonResponse(envelope({ id: "unused" }));
    }));

    renderDashboard();

    const rawPanel = await screen.findByRole("heading", { name: "Raw Atoms" });
    const observationsPanel = await screen.findByRole("heading", { name: "Observations" });

    expect(within(rawPanel.closest("section") as HTMLElement).getByText("Raw turn content")).toBeTruthy();
    expect(within(observationsPanel.closest("section") as HTMLElement).getByText("User prefers concise answers")).toBeTruthy();
  });

  it("renders triple SQL rows as typed triples instead of only generic SQL evidence", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (url.includes("view=stats")) {
        return jsonResponse(envelope({ ready: true, atom_count: 0, session_count: 0, triple_count: 1, tombstoned_count: 0 }));
      }
      if (url.includes("view=recent")) {
        return jsonResponse(envelope({ atoms: [], channels: [] }, { cursor: null, limit: 50, total: 0, truncated: false }));
      }
      if (url.includes("/api/v1/saga/sql") && init?.method === "POST") {
        return jsonResponse(envelope({
          columns: ["subject", "predicate", "object", "confidence"],
          rows: [["user", "prefers", "concise answers", 0.91]],
          row_count: 1,
          truncated: false
        }, { cursor: null, limit: null, total: 1, truncated: false }));
      }
      return jsonResponse(envelope({}));
    }));

    renderDashboard();

    fireEvent.click(await screen.findByRole("tab", { name: "Triples" }));
    fireEvent.click(screen.getByRole("button", { name: "Run Query" }));

    const triplesPanel = await screen.findByRole("heading", { name: "Triples" });
    const panel = triplesPanel.closest("section") as HTMLElement;
    expect(within(panel).getByText("user")).toBeTruthy();
    expect(within(panel).getByText("prefers")).toBeTruthy();
    expect(within(panel).getByText("concise answers")).toBeTruthy();
  });
});
