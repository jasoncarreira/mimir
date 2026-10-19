// @vitest-environment jsdom
import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import * as components from "../../../../frontend/src/ui";
import type { PluginHost, PluginRoute } from "../../../../frontend/src/dashboardExtensions";

const routes: PluginRoute[] = [];
const fetcher = vi.fn();
function reply(data: unknown, status = 200) {
  return Promise.resolve({ ok: status < 400, status, json: async () => status < 400 ? { ok: true, data } : { ok: false, error: { message: "Access denied" } } });
}

beforeAll(async () => {
  window.mimirPluginHost = {
    apiMajor: 1, react: React, uiKit: { version: 1, components },
    authenticatedFetch: fetcher, registerRoute: (route: PluginRoute) => routes.push(route), registerNavEntry: vi.fn()
  } as unknown as PluginHost;
  await import("./main");
});

beforeEach(() => { fetcher.mockReset(); window.history.replaceState(null, "", "/cultivate/factory-runs"); });

describe("Cultivate bundle", () => {
  it("registers only the manifest-backed route and renders empty/error states", async () => {
    expect(routes.map(route => [route.id, route.path])).toEqual([["cultivate-factory-runs", "/cultivate/factory-runs"]]);
    fetcher.mockImplementationOnce(() => reply({ runs: [] })).mockImplementationOnce(() => reply({}, 403));
    const Route = routes[0].component;
    render(<Route />);
    expect(screen.getByText("Loading factory runs")).toBeTruthy();
    expect(await screen.findByText("No factory runs found")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Refresh runs" }));
    expect(await screen.findByText(/Access denied/)).toBeTruthy();
    expect(fetcher.mock.calls[0][0]).toBe("/api/v1/plugins/cultivate/factory-runs");
  });

  it("requires a reason, handles archive refusal, and loads run detail", async () => {
    const run = { run_id: "long-run-id", issue_key: "chainlink-42", clearable: true, valid: true,
      status: "needs-human", mode: "autonomous", branch: "topic", pr_base: "main", lock: "absent",
      controller_phase: "parked", observed_at: null };
    fetcher.mockImplementation((path: string) => {
      if (path.includes("/archive")) return reply({}, 409);
      if (path.includes("/factory-run?")) return reply({ ...run, slices: [], gates: {}, steps: [], validator: null, terminal_result: null });
      return reply({ runs: [run] });
    });
    const Route = routes[0].component;
    render(<Route />);
    expect(await screen.findByText("needs-human")).toBeTruthy();
    fireEvent.click(await screen.findByRole("button", { name: "Archive run long-run-id" }));
    const confirm = screen.getByRole("button", { name: "Confirm archive" });
    expect((confirm as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Archive reason"), { target: { value: "  stale run  " } });
    fireEvent.click(confirm);
    expect(await screen.findByText(/Archive failed/)).toBeTruthy();
    expect(fetcher.mock.calls.find(call => String(call[0]).includes("/archive"))?.[1]?.body).toBe('{"reason":"stale run"}');
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    fireEvent.click(screen.getByRole("button", { name: /long-run-id · chainlink-42/ }));
    expect(await screen.findByText("Run: long-run-id")).toBeTruthy();
    await waitFor(() => expect(screen.getByText("No slices reported.")).toBeTruthy());
  });

  it("uses the owner-scoped PR endpoint and does not create unsafe links", async () => {
    fetcher.mockImplementation(() => reply({ available: true, repo: "owner/repo", truncated: false,
      pull_requests: [{ number: 4, title: "Review", url: "javascript:alert(1)", created_at: "", review_decision: "", is_draft: true }] }));
    render(React.createElement(window.mimirCultivate!.PullRequests));
    expect(await screen.findByText("Review")).toBeTruthy();
    expect(screen.queryByRole("link", { name: "Open PR 4" })).toBeNull();
    expect(fetcher.mock.calls[0][0]).toBe("/api/v1/plugins/cultivate/pr-board");
  });
});
