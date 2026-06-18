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
          schema_version: 6,
          db_size_bytes: 1536
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
              source_type: "agent_authored",
              session_id: "session-1",
              topics: ["runtime", "memory"],
              is_pinned: true,
              tombstoned: false,
              last_access_source: "query",
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

    expect(screen.getByText("1.5 KB")).toBeTruthy();
    expect(within(rawPanel.closest("section") as HTMLElement).getByText("Raw turn content")).toBeTruthy();
    expect(within(observationsPanel.closest("section") as HTMLElement).getByText("User prefers concise answers")).toBeTruthy();
  });

  it("renders legacy atom detail fields instead of silently dropping them", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (url.includes("view=stats")) {
        return jsonResponse(envelope({ ready: true, atom_count: 1, session_count: 1, triple_count: 0, tombstoned_count: 0, db_size_bytes: 2048 }));
      }
      if (url.includes("view=recent")) {
        return jsonResponse(envelope({
          atoms: [{ id: "raw-1", content_preview: "Raw turn content", memory_type: "raw", stream: "episodic" }],
          channels: []
        }, { cursor: "raw-1", limit: 50, total: 1, truncated: false }));
      }
      if (url.includes("view=atom") && url.includes("raw-1")) {
        return jsonResponse(envelope({
          id: "raw-1",
          content: "Full atom body",
          memory_type: "raw",
          source_type: "agent_authored",
          stream: "episodic",
          session_id: "session-1",
          channel_id: "discord-123",
          topics: ["runtime", "memory"],
          encoding_confidence: 0.875,
          is_pinned: 1,
          tombstoned: 0,
          tombstoned_reason: null,
          access_count: 3,
          last_access_ts: "2026-06-18T00:02:00Z",
          last_access_source: "saga_query",
          arousal: 0.12,
          valence: -0.03
        }));
      }
      return jsonResponse(envelope({}));
    }));

    renderDashboard();

    const rawPanel = (await screen.findByRole("heading", { name: "Raw Atoms" })).closest("section") as HTMLElement;
    fireEvent.click(within(rawPanel).getByRole("button", { name: "Inspect" }));

    expect(await screen.findByText("agent_authored")).toBeTruthy();
    expect(screen.getByText("session-1")).toBeTruthy();
    expect(screen.getByText("runtime, memory")).toBeTruthy();
    expect(screen.getByText("0.875")).toBeTruthy();
    expect(screen.getAllByText("yes").length).toBeGreaterThan(0);
    expect(screen.getByText("saga_query")).toBeTruthy();
    expect(screen.getByText("Full atom body")).toBeTruthy();
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
