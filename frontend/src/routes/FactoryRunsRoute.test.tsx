// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import type React from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type {
  ApiSuccessEnvelope,
  FactoryRunDetail,
  FactoryRunsData,
  FactoryRunSummary,
  ListMeta
} from "../api/generated/contracts";
import type { DashboardSurface } from "../dashboardExtensions";

const { factoryApi } = vi.hoisted(() => ({
  factoryApi: { getFactoryRun: vi.fn(), getFactoryRuns: vi.fn() }
}));

vi.mock("../api/factory-runs", async (original) => ({
  ...(await original<Record<string, unknown>>()),
  getFactoryRun: factoryApi.getFactoryRun,
  getFactoryRuns: factoryApi.getFactoryRuns
}));

const { FactoryRunsRoute, RunDetail } = await import("./FactoryRunsRoute");

const surface: DashboardSurface = {
  id: "factory-runs",
  route_path: "/factory-runs",
  path: "/factory-runs",
  label: "Factory runs",
  title: "Factory runs",
  detail: "Feature factory runs and diagnostics",
  icon: null,
  nav_position: 1,
  enabled: true,
  bundle: null,
  css: [],
  api_namespace: "factory-runs",
  trusted_first_party: true,
  tabs: ["list", "detail"],
  filterLabel: "status"
};

const baseFactoryRun = {
  run_id: "834",
  issue_key: "834",
  valid: true,
  sandbox_path: "/srv/mimir/factory/834",
  status: "running",
  mode: "autonomous",
  branch: "slice/834-factory-run",
  pr_base: "main",
  pr_draft: true,
  lock: "fresh",
  dead_lock: false,
  lock_session: "session-834",
  pr_url: null,
  next: "build",
  controller_phase: "monitoring",
  observed_at: "2026-07-13T10:00:00Z",
  controller_error: null
} satisfies FactoryRunSummary;

const factoryRunsListFixture: ApiSuccessEnvelope<FactoryRunsData, ListMeta> = {
  ok: true,
  version: "v1",
  data: {
    runs: [
      baseFactoryRun,
      {
        ...baseFactoryRun,
        run_id: "833",
        issue_key: "833",
        sandbox_path: "/srv/mimir/factory/833",
        status: "completed",
        branch: "slice/833-factory-run",
        pr_draft: false,
        lock: "absent",
        lock_session: null,
        pr_url: "https://github.com/owner/repo/pull/42",
        next: null,
        controller_phase: "completed",
        observed_at: "2026-07-12T15:30:00Z"
      },
      {
        ...baseFactoryRun,
        run_id: "832",
        issue_key: "832",
        sandbox_path: "/srv/mimir/factory/832",
        status: "needs-human",
        branch: "slice/832-factory-run",
        lock: "stale",
        dead_lock: true,
        lock_session: "session-832",
        next: "resume",
        controller_phase: "parked",
        observed_at: "2026-07-11T09:00:00Z",
        controller_error: "Operator input required"
      },
      {
        ...baseFactoryRun,
        run_id: "831",
        issue_key: "831",
        valid: false,
        sandbox_path: "/srv/mimir/factory/831",
        branch: "slice/831-factory-run",
        lock: "absent",
        lock_session: null,
        next: null,
        controller_phase: "failed",
        observed_at: null,
        controller_error: "Factory status validation failed"
      }
    ]
  },
  meta: {
    cursor: null,
    limit: null,
    total: 4,
    truncated: false
  }
};

const factoryRunDetailFixture: ApiSuccessEnvelope<FactoryRunDetail> = {
  ok: true,
  version: "v1",
  data: {
    ...baseFactoryRun,
    gates: { story: "approved", brief: "approved" },
    steps: ["spec-writer:accepted", "work-decomposer:running"],
    slices: ["s1:merged", "s2:building"],
    validator: "GO-WITH-NITS",
    terminal_result: null
  }
};

function renderRoute(node: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{node}</MemoryRouter>
    </QueryClientProvider>
  );
}

afterEach(() => {
  cleanup();
  factoryApi.getFactoryRun.mockReset();
  factoryApi.getFactoryRuns.mockReset();
});

describe("FactoryRunsRoute", () => {
  it.each([
    { status: "running", valid: true, phase: "monitoring", display: "running", lifecycle: "Active" },
    { status: "completed", valid: true, phase: "terminal", display: "completed", lifecycle: "Terminal" },
    { status: "needs-human", valid: true, phase: "parked", display: "needs-human", lifecycle: "Parked/resumable" },
    { status: null, valid: false, phase: "running", display: "unavailable", lifecycle: "Unavailable" },
    { status: "running", valid: false, phase: "monitoring", display: "unavailable", lifecycle: "Unavailable" },
    { status: "pending", valid: true, phase: "starting", display: "pending", lifecycle: "Unavailable" },
    { status: "unknown", valid: true, phase: "unknown", display: "unknown", lifecycle: "Unavailable" },
    { status: "running", valid: true, phase: "stopped", display: "running", lifecycle: "Unavailable" },
    { status: null, valid: false, phase: "failed", display: "failed", lifecycle: "Failed" },
    { status: "running", valid: false, phase: "failed", display: "failed", lifecycle: "Failed" },
    { status: "running", valid: true, phase: "failed", display: "failed", lifecycle: "Failed" }
  ])("distinguishes projection $status/$valid from controller $phase", async ({ status, valid, phase, display, lifecycle }) => {
    const run = { ...factoryRunDetailFixture.data, status, valid, controller_phase: phase };
    factoryApi.getFactoryRun.mockResolvedValue({ ...factoryRunDetailFixture, data: run });
    factoryApi.getFactoryRuns.mockResolvedValue({ ...factoryRunsListFixture, data: { runs: [run] } });
    renderRoute(<><FactoryRunsRoute surface={surface} /><RunDetail runId={run.run_id} /></>);

    const card = await screen.findByTestId(`factory-run-${run.run_id}`);
    expect(within(card).getByText(display)).toBeTruthy();
    const detail = await screen.findByTestId("factory-run-detail");
    const fact = (label: string) => within(detail).getByText(label, { selector: "dt" }).nextElementSibling?.textContent;
    expect(fact("Status")).toBe(display);
    expect(fact("Lifecycle")).toBe(lifecycle);
    expect(fact("Projected status")).toBe(status ?? "not available");
    expect(fact("Controller phase")).toBe(phase);
    if (lifecycle !== "Active") expect(within(detail).queryByText("Active")).toBeNull();
  });

  it("shows the API's durable issue fallback in list and detail", async () => {
    const run = { ...factoryRunDetailFixture.data, run_id: "chainlink-1521", issue_key: "1521", status: "completed", controller_phase: "terminal" };
    factoryApi.getFactoryRun.mockResolvedValue({ ...factoryRunDetailFixture, data: run });
    factoryApi.getFactoryRuns.mockResolvedValue({ ...factoryRunsListFixture, data: { runs: [run] } });
    renderRoute(<><FactoryRunsRoute surface={surface} /><RunDetail runId={run.run_id} /></>);
    expect(await screen.findByText("chainlink-1521 · 1521")).toBeTruthy();
    const detail = await screen.findByTestId("factory-run-detail");
    expect(within(detail).getByText("Issue").nextElementSibling?.textContent).toBe("1521");
  });

  it("renders Worklink status, parked recovery, and invalid projection state", async () => {
    factoryApi.getFactoryRuns.mockResolvedValue(factoryRunsListFixture);

    renderRoute(<FactoryRunsRoute surface={surface} />);

    const parkedRun = await screen.findByTestId("factory-run-832");
    expect(within(parkedRun).getByText("needs-human")).toBeTruthy();
    expect(within(parkedRun).getByText("parked/resumable")).toBeTruthy();
    expect(within(parkedRun).getByText("dead lock")).toBeTruthy();
    expect(within(parkedRun).queryByText(/terminal:/i)).toBeNull();

    const invalidRun = screen.getByTestId("factory-run-831");
    expect(within(invalidRun).getByText("invalid projection")).toBeTruthy();
    expect(screen.queryByText(/heartbeat|security|pending gate/i)).toBeNull();
  });

  it("uses only top-level status for terminal display and keeps terminal context inert", async () => {
    factoryApi.getFactoryRun.mockResolvedValue({
      ...factoryRunDetailFixture,
      data: {
        ...factoryRunDetailFixture.data,
        status: "running",
        next: "observe",
        pr_url: "https://github.com/owner/repo/pull/7",
        terminal_result: {
          status: "completed",
          reason: "resume-from-terminal",
          pr_url: "https://github.com/owner/repo/pull/unsafe"
        }
      }
    });

    renderRoute(<RunDetail runId="834" />);

    expect(await screen.findByText("Active")).toBeTruthy();
    expect(screen.getByText("GO-WITH-NITS")).toBeTruthy();
    expect(screen.queryByText("Terminal")).toBeNull();
    expect(screen.getByText("observe")).toBeTruthy();
    expect(screen.getByText(/resume-from-terminal/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "https://github.com/owner/repo/pull/7" })).toBeTruthy();
    expect(screen.queryByRole("link", { name: "https://github.com/owner/repo/pull/unsafe" })).toBeNull();
    expect(screen.getByText("Cost attribution is unavailable for this factory projection.")).toBeTruthy();
    expect(screen.queryByText(/total tokens|cost total|requests/i)).toBeNull();
  });

  it("renders an unsafe top-level PR URL as text instead of a link", async () => {
    factoryApi.getFactoryRun.mockResolvedValue({
      ...factoryRunDetailFixture,
      data: {
        ...factoryRunDetailFixture.data,
        status: "completed",
        pr_url: "javascript:alert('run')",
        terminal_result: {
          status: "running",
          reason: "not-authoritative",
          pr_url: "javascript:alert('terminal')"
        }
      }
    });

    renderRoute(<RunDetail runId="834" />);

    await waitFor(() => expect(screen.getByText("javascript:alert('run')")).toBeTruthy());
    expect(screen.getByText("Terminal")).toBeTruthy();
    expect(screen.queryAllByRole("link", { name: /javascript:alert/ })).toHaveLength(0);
  });
});
