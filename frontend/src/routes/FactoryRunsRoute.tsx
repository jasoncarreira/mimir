import { useQuery } from "@tanstack/react-query";
import React from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  getFactoryRun,
  getFactoryRuns,
  type FactoryRunDetail,
  type FactoryRunSummary
} from "../api/factory-runs";
import type { DashboardSurface } from "../dashboardExtensions";
import { sanitizeHref } from "../routeState";
import {
  Badge,
  CodeBlock,
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
  blocked: "danger",
  partial: "warning",
  "needs-human": "warning",
  interrupted: "warning",
  invalid: "danger",
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
  const status = run.status || "unknown";
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
  const status = run.status || "unknown";
  const terminal = isTerminalStatus(status);
  const parked = status === "needs-human";
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
          <div><dt>Lifecycle</dt><dd>{parked ? "Parked/resumable" : terminal ? "Terminal" : "Active"}</dd></div>
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
          {run.controller_error ? <div><dt>Controller error</dt><dd>{run.controller_error}</dd></div> : null}
        </dl>
      </Panel>

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

      <Panel title="Slices">
        <CompactList className="factory-slices" items={run.slices} />
      </Panel>

      <Panel title="Validator">
        {run.validator
          ? <OpaqueContext value={run.validator} />
          : <p className="app-copy">No validator context reported.</p>}
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
  );
}

export function FactoryRunsRoute({ surface }: FactoryRunsRouteProps) {
  const [searchParams, setSearchParams] = useSearchParams();
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
          <RunCard
            key={run.run_id}
            onClick={() => {
              const params = new URLSearchParams(searchParams);
              params.set("run", run.run_id);
              setSearchParams(params);
            }}
            run={run}
          />
        ))}
      </div>
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
