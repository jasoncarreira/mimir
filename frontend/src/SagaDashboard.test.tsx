// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import React from "react";
import { MemoryRouter } from "react-router-dom";
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
      <MemoryRouter>
        <SagaDashboard />
      </MemoryRouter>
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

  it("renders mixed atoms and observations together in one combined list", async () => {
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

    const panel = (await screen.findByRole("heading", { name: "Atoms & Observations" })).closest("section") as HTMLElement;
    const list = within(panel).getByRole("list", { name: "Atoms and observations" });

    expect(screen.getByText("1.5 KB")).toBeTruthy();
    // Both kinds share one list, one line each, each carrying its kind pill.
    expect(within(list).getByText("Raw turn content")).toBeTruthy();
    expect(within(list).getByText("User prefers concise answers")).toBeTruthy();
    expect(within(list).getByText("atom")).toBeTruthy();
    expect(within(list).getByText("observation")).toBeTruthy();
  });

  it("pops out legacy atom detail fields on click instead of dropping them", async () => {
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

    // Click the atom row -> detail pops out in the drawer.
    fireEvent.click(await screen.findByRole("button", { name: /Raw turn content/ }));

    expect(await screen.findByText("agent_authored")).toBeTruthy();
    expect(screen.getByText("session-1")).toBeTruthy();
    expect(screen.getByText("runtime, memory")).toBeTruthy();
    expect(screen.getByText("0.875")).toBeTruthy();
    expect(screen.getAllByText("yes").length).toBeGreaterThan(0);
    expect(screen.getByText("saga_query")).toBeTruthy();
    expect(screen.getByText("Full atom body")).toBeTruthy();
  });

  it("filters by type and surfaces triples with click-to-detail (#574)", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (url.includes("view=stats")) {
        return jsonResponse(envelope({ ready: true, atom_count: 2, triple_count: 1 }));
      }
      if (url.includes("view=recent")) {
        return jsonResponse(envelope({
          atoms: [
            { id: "raw-1", content_preview: "Raw turn content", memory_type: "raw", created_at: "2026-06-18T00:00:00Z" },
            { id: "obs-1", content_preview: "User prefers concise answers", memory_type: "observation", created_at: "2026-06-18T00:01:00Z" }
          ],
          channels: []
        }, { cursor: "obs-1", limit: 50, total: 2, truncated: false }));
      }
      if (url.includes("/api/v1/saga/sql") && init?.method === "POST") {
        return jsonResponse(envelope({
          columns: ["id", "subject", "predicate", "object", "confidence"],
          rows: [["tr-1", "user", "prefers", "concise answers", 0.91]],
          row_count: 1,
          truncated: false
        }, { cursor: null, limit: null, total: 1, truncated: false }));
      }
      return jsonResponse(envelope({}));
    }));

    renderDashboard();

    // Unified list shows both kinds by default.
    expect(await screen.findByText("Raw turn content")).toBeTruthy();
    expect(screen.getByText("User prefers concise answers")).toBeTruthy();

    // Filter to Observations -> the raw atom drops out.
    fireEvent.change(screen.getByLabelText("Type"), { target: { value: "observation" } });
    expect(screen.queryByText("Raw turn content")).toBeNull();
    expect(screen.getByText("User prefers concise answers")).toBeTruthy();

    // Switch to Triples -> triples become visible; click opens the triple detail.
    fireEvent.change(screen.getByLabelText("Type"), { target: { value: "triple" } });
    const tripleRow = await screen.findByRole("button", { name: /prefers/ });
    fireEvent.click(tripleRow);
    expect(await screen.findByText("Triple Detail")).toBeTruthy();
    expect(screen.getByText("concise answers")).toBeTruthy();
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
