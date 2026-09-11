import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import React from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  archiveFactoryRun,
  getFactoryRun,
  getFactoryRuns,
  type FactoryRunDetail,
  type FactoryRunSummary
} from "../api/factory-runs";
import type { DashboardSurface } from "../dashboardExtensions";
import { sanitizeHref } from "../routeState";
import {
  Badge,
  Button,
  CodeBlock,
  Dialog,
  EmptyState,
  ErrorState,
  LoadingState,
  Panel
} from "../ui";

interface FactoryRunsRouteProps {
  surface: DashboardSurface;
}

type BadgeTone = React.ComponentProps<typeof Badge>["tone"];

const statusTone: Record<string, BadgeTone> = {
  running: "info",
  completed: "success",
  merged: "success",
  building: "info",
  queued: "neutral",
  skipped: "neutral",
  blocked: "danger",
  partial: "warning",
  "needs-human": "warning",
  interrupted: "warning",
  invalid: "danger",
  failed: "danger",
  unavailable: "warning",
  pending: "neutral",
  unknown: "neutral"
};

const lockTone: Record<FactoryRunSummary["lock"], BadgeTone> = {
  fresh: "success",
  stale: "warning",
  absent: "neutral"
};

function isTerminalStatus(status: string): boolean {
  return status === "completed" || status === "blocked" || status === "partial";
}

function displayStatus(run: FactoryRunSummary): string {
  if (run.controller_phase === "failed") return "failed";
  if (!run.valid || !run.status) return "unavailable";
  return run.status;
}

function formatTime(iso: string | null): string {
  if (!iso) return "not observed";
  const timestamp = Date.parse(iso);
  return Number.isNaN(timestamp) ? iso : new Date(timestamp).toLocaleString();
}

function OpaqueContext({ value }: { value: Record<string, unknown> }) {
  return <CodeBlock code={JSON.stringify(value, null, 2)} language="json" />;
}

function CompactList({ items, className }: { items: string[]; className: string }) {
  if (items.length === 0) return <p className="app-copy">None reported.</p>;
  return (
    <div className={className}>
      {items.map((item, index) => (
        <Badge key={`${index}-${item}`}>{item}</Badge>
      ))}
    </div>
  );
}

function RunCard({ run, onClick }: { run: FactoryRunSummary; onClick: () => void }) {
  const status = displayStatus(run);
  const terminal = isTerminalStatus(status);

  return (
    <button
      className="factory-run-card"
      data-testid={`factory-run-${run.run_id}`}
      onClick={onClick}
      type="button"
    >
      <span className="factory-run-card__id">{run.run_id} · {run.issue_key}</span>
      <span className="factory-run-card__badges">
        <Badge tone={statusTone[status] ?? "neutral"}>{status}</Badge>
        <Badge tone={run.valid ? "success" : "danger"}>{run.valid ? "valid" : "invalid projection"}</Badge>
        <Badge tone={lockTone[run.lock]}>lock {run.lock}</Badge>
        {run.dead_lock ? <Badge tone="danger">dead lock</Badge> : null}
        {status === "needs-human" ? <Badge tone="warning">parked/resumable</Badge> : null}
        {run.pr_draft ? <Badge tone="warning">draft PR</Badge> : null}
      </span>
      <span className="factory-run-card__meta">
        <span>{run.mode} · {run.branch} → {run.pr_base}</span>
        <span>Controller: {run.controller_phase || "unknown"}</span>
        <span>Observed: {formatTime(run.observed_at)}</span>
        {run.pr_url ? <span>PR: {run.pr_url}</span> : null}
        {run.controller_error ? <span className="factory-run-card__error">{run.controller_error}</span> : null}
      </span>
      {terminal ? <span className="factory-run-card__terminal">Terminal: {status}</span> : null}
    </button>
  );
}

export function RunDetail({ runId }: { runId: string }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["factory-run", runId],
    queryFn: async () => (await getFactoryRun(runId)).data
  });

  if (isLoading) return <LoadingState label="Loading run details" />;
  if (error) return <ErrorState title="Failed to load run">{String(error)}</ErrorState>;
  if (!data) return <EmptyState title="Run not found" />;

  const run = data as FactoryRunDetail;
  const status = displayStatus(run);
  const terminal = isTerminalStatus(status);
  const parked = status === "needs-human";
  const active = status === "running" && ["running", "monitoring"].includes(run.controller_phase);
  const prHref = sanitizeHref(run.pr_url);
  const hasNext = Object.prototype.hasOwnProperty.call(run, "next");

  return (
    <div className="factory-run-detail" data-testid="factory-run-detail">
      <Panel
        actions={<Link className="ui-button ui-button--secondary" to="/factory-runs">Back to list</Link>}
        title={`Run: ${run.run_id}`}
      >
        <dl className="facts-grid">
          <div><dt>Issue</dt><dd>{run.issue_key}</dd></div>
          <div><dt>Status</dt><dd><Badge tone={statusTone[status] ?? "neutral"}>{status}</Badge></dd></div>
          <div><dt>Lifecycle</dt><dd>{status === "failed" ? "Failed" : parked ? "Parked/resumable" : terminal ? "Terminal" : active ? "Active" : "Unavailable"}</dd></div>
        </dl>
      </Panel>

      <Panel title="Slices" id="factory-slices">
        {run.slices.length ? (
          <table className="factory-slices" aria-label="Slice progress">
            <thead><tr><th scope="col">Name</th><th scope="col">Status</th><th scope="col">Attempt</th></tr></thead>
            <tbody>
              {run.slices.map((slice, index) => {
                // The projection may omit attempts; retain unfamiliar formats verbatim.
                const match = /^([^:]+):([^:()]+?)(?:\((\d+)\))?$/.exec(slice);
                const name = match?.[1] ?? slice;
                const sliceStatus = match?.[2] ?? "unknown";
                return (
                  <tr key={`${index}-${slice}`}>
                    <th scope="row">{name}</th>
                    <td><Badge tone={Object.hasOwn(statusTone, sliceStatus) ? statusTone[sliceStatus] : "neutral"}>{sliceStatus}</Badge></td>
                    <td>{match?.[3] ?? "Not reported"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        ) : <p className="app-copy">No slices reported.</p>}
      </Panel>

      <details className="factory-diagnostics">
        <summary>Run diagnostics{run.controller_error ? " (controller error reported)" : ""}</summary>
        <div className="factory-diagnostics__body" role="region" aria-label="Run diagnostics" tabIndex={0}>
          <Panel title="Run facts">
            <dl className="facts-grid">
              <div><dt>Projected status</dt><dd>{run.status ?? "not available"}</dd></div>
              <div><dt>Valid projection</dt><dd>{run.valid ? "Yes" : "No"}</dd></div>
              <div><dt>Mode</dt><dd>{run.mode}</dd></div>
              <div><dt>Branch</dt><dd>{run.branch}</dd></div>
              <div><dt>Base</dt><dd>{run.pr_base}</dd></div>
              <div><dt>Draft PR</dt><dd>{run.pr_draft ? "Yes" : "No"}</dd></div>
              <div><dt>Controller phase</dt><dd>{run.controller_phase || "unknown"}</dd></div>
              <div><dt>Observed</dt><dd>{formatTime(run.observed_at)}</dd></div>
              <div><dt>Sandbox</dt><dd>{run.sandbox_path}</dd></div>
              {hasNext ? <div><dt>Next action</dt><dd>{run.next || "none"}</dd></div> : null}
              {run.pr_url ? (
                <div>
                  <dt>PR URL</dt>
                  <dd>{prHref ? <a href={prHref} rel="noopener noreferrer" target="_blank">{run.pr_url}</a> : run.pr_url}</dd>
                </div>
              ) : null}
            </dl>
          </Panel>

          {run.controller_error ? <Panel title="Controller error"><pre className="factory-controller-error">{run.controller_error}</pre></Panel> : null}

          <Panel title="Lock and session">
            <dl className="facts-grid">
              <div><dt>Lock</dt><dd><Badge tone={lockTone[run.lock]}>{run.lock}</Badge></dd></div>
              <div><dt>Dead lock</dt><dd>{run.dead_lock ? "Yes" : "No"}</dd></div>
              <div><dt>Session</dt><dd>{run.lock_session || "none"}</dd></div>
            </dl>
          </Panel>

          <Panel title="Gates">
            {Object.keys(run.gates).length > 0
              ? <OpaqueContext value={run.gates} />
              : <p className="app-copy">No gate context reported.</p>}
          </Panel>

          <Panel title="Steps">
            <CompactList className="factory-steps" items={run.steps} />
          </Panel>

          <Panel title="Validator">
            {run.validator
              ? <Badge tone={run.validator === "NO-GO" ? "danger" : run.validator === "GO-WITH-NITS" ? "warning" : "success"}>{run.validator}</Badge>
              : <p className="app-copy">No validator verdict reported.</p>}
          </Panel>

          <Panel title="Terminal context">
            {run.terminal_result
              ? <div data-testid="factory-terminal-context"><OpaqueContext value={run.terminal_result} /></div>
              : <p className="app-copy">No terminal context reported.</p>}
          </Panel>

          <Panel title="Cost">
            <p className="app-copy">Cost attribution is unavailable for this factory projection.</p>
          </Panel>
        </div>
      </details>
    </div>
  );
}

export function FactoryRunsRoute({ surface }: FactoryRunsRouteProps) {
  const [searchParams, setSearchParams] = useSearchParams();
  const client = useQueryClient();
  const [archiveRunId, setArchiveRunId] = React.useState<string | null>(null);
  const [reason, setReason] = React.useState("");
  const archiveInFlight = React.useRef(false);
  const archive = useMutation({
    mutationFn: ({ runId, reason }: { runId: string; reason: string }) => archiveFactoryRun(runId, { reason }),
    retry: false,
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ["factory-runs"] });
      setArchiveRunId(null);
    },
    onSettled: () => { archiveInFlight.current = false; }
  });
  const closeArchive = () => {
    if (!archiveInFlight.current) setArchiveRunId(null);
  };
  const runId = searchParams.get("run");
  const { data, isLoading, error } = useQuery({
    queryKey: ["factory-runs"],
    queryFn: async () => (await getFactoryRuns()).data
  });

  if (runId) return <RunDetail runId={runId} />;
  if (isLoading) return <LoadingState label="Loading factory runs" />;
  if (error) return <ErrorState title="Failed to load factory runs">{String(error)}</ErrorState>;

  const runs = data?.runs || [];

  if (runs.length === 0) {
    return (
      <div className="factory-runs">
        <DashboardHeader surface={surface} />
        <EmptyState title="No factory runs found" />
      </div>
    );
  }

  return (
    <div className="factory-runs">
      <DashboardHeader surface={surface} />
      <div className="factory-runs__list">
        {runs.map((run) => (
          <div key={run.run_id}>
            <RunCard
              onClick={() => {
                const params = new URLSearchParams(searchParams);
                params.set("run", run.run_id);
                setSearchParams(params);
              }}
              run={run}
            />
            {run.clearable ? (
              <Button
                aria-label={`Archive run ${run.run_id}`}
                disabled={archive.isPending}
                onClick={() => {
                  archive.reset();
                  setReason("");
                  setArchiveRunId(run.run_id);
                }}
              >Archive</Button>
            ) : null}
          </div>
        ))}
      </div>
      <Dialog open={archiveRunId !== null} title={`Archive run ${archiveRunId}?`} onClose={closeArchive}>
        <p className="app-copy">Archive run {archiveRunId} to remove it from this list. This does not delete the run.</p>
        <form onSubmit={(event) => {
          event.preventDefault();
          if (!archiveRunId || !reason.trim() || archiveInFlight.current) return;
          archiveInFlight.current = true;
          archive.mutate({ runId: archiveRunId, reason: reason.trim() });
        }}>
          <label>
            Archive reason
            <textarea className="ui-input" required disabled={archive.isPending} value={reason} onChange={(event) => setReason(event.target.value)} />
          </label>
          <div className="route-state-form__actions">
            <Button type="button" disabled={archive.isPending} onClick={closeArchive}>Cancel</Button>
            <Button type="submit" variant="primary" disabled={archive.isPending || !reason.trim()}>
              {archive.isPending ? "Archiving..." : "Confirm archive"}
            </Button>
          </div>
        </form>
        {archive.error ? <ErrorState title="Archive failed">{archive.error.message}</ErrorState> : null}
      </Dialog>
    </div>
  );
}

function DashboardHeader({ surface }: { surface: DashboardSurface }) {
  return (
    <header className="dashboard-header">
      <h1>{surface.title}</h1>
      <p className="dashboard-header__detail">{surface.detail}</p>
    </header>
  );
}
