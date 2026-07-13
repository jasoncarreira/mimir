import { useQuery } from "@tanstack/react-query";
import React from "react";
import { Link, useSearchParams } from "react-router-dom";
import { getFactoryRun, getFactoryRuns, type FactoryRunSummary, type FactoryRunDetail } from "../api/factory-runs";
import type { DashboardSurface } from "../dashboardExtensions";
import {
  Badge,
  Button,
  EmptyState,
  ErrorState,
  LoadingState,
  Panel
} from "../ui";
import type { ApiError } from "../api/http";

interface FactoryRunsRouteProps {
  surface: DashboardSurface;
}

const statusTone: Record<string, React.ComponentProps<typeof Badge>["tone"]> = {
  running: "info",
  completed: "success",
  blocked: "danger",
  partial: "warning",
  "needs-human": "warning",
  invalid: "danger",
  unknown: "neutral",
};

function formatTime(iso: string): string {
  if (!iso) return "never";
  try {
    const date = new Date(iso);
    return date.toLocaleString();
  } catch {
    return iso;
  }
}

function RunCard({
  run,
  onClick
}: {
  run: FactoryRunSummary;
  onClick: () => void;
}) {
  const status = run.status || "unknown";
  const isStale = run.is_stale;
  const isTerminal = run.is_terminal;
  const hasError = run.error || (run.diagnostic && status === "invalid");

  return (
    <button className="factory-run-card" onClick={onClick} type="button">
      <span className="factory-run-card__id">{run.run_id}</span>
      <span className="factory-run-card__badges">
        <Badge tone={statusTone[status] ?? "neutral"}>{status}</Badge>
        {isStale && <Badge tone="danger">stale</Badge>}
        {run.is_stale && run.status === "running" && <Badge tone="warning">stale heartbeat</Badge>}
        {run.pending_gate && <Badge tone="warning">gate: {run.pending_gate}</Badge>}
        {run.validator_verdict && <Badge tone={run.validator_verdict === "GO" ? "success" : "danger"}>validator: {run.validator_verdict}</Badge>}
        {run.security_verdict && <Badge tone={run.security_verdict === "PASS" ? "success" : "danger"}>security: {run.security_verdict}</Badge>}
      </span>
      <span className="factory-run-card__meta">
        <span>Heartbeat: {formatTime(run.heartbeat_at)}</span>
        {run.pr_url && <span>PR: {run.pr_url}</span>}
        {run.error && <span className="factory-run-card__error">{run.error}</span>}
      </span>
      {run.terminal_result && (
        <span className="factory-run-card__terminal">
          Terminal: {run.terminal_result.status}
          {run.terminal_result.reason && ` - ${run.terminal_result.reason}`}
        </span>
      )}
    </button>
  );
}

function GateStatus({ statuses }: { statuses: Array<[string, string]> }) {
  if (!statuses || statuses.length === 0) return null;
  return (
    <div className="factory-gates">
      <dt>Gates</dt>
      <dd>
        {statuses.map(([name, status]) => (
          <Badge key={name} tone={status === "approved" ? "success" : status === "pending" ? "warning" : "neutral"}>
            {name}: {status}
          </Badge>
        ))}
      </dd>
    </div>
  );
}

function RunDetail({ runId }: { runId: string }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["factory-run", runId],
    queryFn: async () => {
      const response = await getFactoryRun(runId);
      return response.data;
    },
  });

  if (isLoading) return <LoadingState label="Loading run details" />;
  if (error) return <ErrorState title="Failed to load run">{String(error)}</ErrorState>;
  if (!data) return <EmptyState title="Run not found" />;

  const run = data as FactoryRunDetail;
  const status = run.status || "unknown";

  return (
    <div className="factory-run-detail">
      <Panel
        title={`Run: ${run.run_id}`}
        actions={
          <Link to="/factory-runs">
            <Button variant="secondary">Back to List</Button>
          </Link>
        }
      >
        <dl className="facts-grid">
          <div><dt>Status</dt><dd><Badge tone={statusTone[status] ?? "neutral"}>{status}</Badge></dd></div>
          <div><dt>Terminal</dt><dd>{run.is_terminal ? "Yes" : "No"}</dd></div>
          <div><dt>Stale</dt><dd>{run.is_stale ? "Yes" : "No"}</dd></div>
          <div><dt>Heartbeat</dt><dd>{formatTime(run.heartbeat_at)}</dd></div>
          {run.pr_url && <div><dt>PR URL</dt><dd><a href={run.pr_url} target="_blank" rel="noopener noreferrer">{run.pr_url}</a></dd></div>}
          {run.error && <div><dt>Error</dt><dd>{run.error}</dd></div>}
          {run.pending_gate && <div><dt>Pending Gate</dt><dd>{run.pending_gate}</dd></div>}
          {run.validator_verdict && <div><dt>Validator</dt><dd><Badge tone={run.validator_verdict === "GO" ? "success" : "danger"}>{run.validator_verdict}</Badge></dd></div>}
          {run.security_verdict && <div><dt>Security</dt><dd><Badge tone={run.security_verdict === "PASS" ? "success" : "danger"}>{run.security_verdict}</Badge></dd></div>}
        </dl>
      </Panel>

      <Panel title="Gates">
        <GateStatus statuses={run.gate_statuses} />
      </Panel>

      {run.steps && run.steps.length > 0 && (
        <Panel title="Steps">
          <div className="factory-steps">
            {run.steps.map(([agent, status]) => (
              <div key={agent} className="factory-step">
                <Badge tone={status === "accepted" || status === "completed" ? "success" : status === "running" ? "warning" : "neutral"}>
                  {agent}: {status}
                </Badge>
              </div>
            ))}
          </div>
        </Panel>
      )}

      {run.slices && run.slices.length > 0 && (
        <Panel title="Slices">
          <div className="factory-slices">
            {run.slices.map(([id, status]) => (
              <div key={id} className="factory-slice">
                <Badge tone={status === "merged" ? "success" : status === "building" || status === "running" ? "warning" : "neutral"}>
                  {id}: {status}
                </Badge>
              </div>
            ))}
          </div>
        </Panel>
      )}

      {run.terminal_result && (
        <Panel title="Terminal Result">
          <dl className="facts-grid">
            <div><dt>Status</dt><dd>{run.terminal_result.status}</dd></div>
            {run.terminal_result.pr_url && <div><dt>PR URL</dt><dd><a href={run.terminal_result.pr_url} target="_blank" rel="noopener noreferrer">{run.terminal_result.pr_url}</a></dd></div>}
            {run.terminal_result.reason && <div><dt>Reason</dt><dd>{run.terminal_result.reason}</dd></div>}
            {run.terminal_result.summary && <div><dt>Summary</dt><dd>{run.terminal_result.summary}</dd></div>}
          </dl>
        </Panel>
      )}

      {run.cost && (
        <Panel title="Cost Attribution">
          <dl className="facts-grid">
            <div><dt>Status</dt><dd>{run.cost.status}</dd></div>
            {run.cost.total_tokens !== null && <div><dt>Total Tokens</dt><dd>{run.cost.total_tokens.toLocaleString()}</dd></div>}
            {run.cost.cost_total !== null && <div><dt>Cost Total</dt><dd>{run.cost.cost_total.toFixed(6)} {run.cost.cost_currency}</dd></div>}
            {run.cost.request_count !== null && <div><dt>Requests</dt><dd>{run.cost.request_count}</dd></div>}
          </dl>
        </Panel>
      )}

      {run.debug && (
        <Panel title="Debug Info">
          <dl className="facts-grid">
            {run.debug.created_at && <div><dt>Created</dt><dd>{formatTime(run.debug.created_at)}</dd></div>}
            {run.debug.resumed_at && <div><dt>Resumed</dt><dd>{formatTime(run.debug.resumed_at)}</dd></div>}
            {run.debug.resume_count !== null && <div><dt>Resume Count</dt><dd>{run.debug.resume_count}</dd></div>}
          </dl>
        </Panel>
      )}
    </div>
  );
}

export function FactoryRunsRoute({ surface }: FactoryRunsRouteProps) {
  const [searchParams, setSearchParams] = useSearchParams();
  const runId = searchParams.get("run");

  const { data, isLoading, error } = useQuery({
    queryKey: ["factory-runs"],
    queryFn: async () => {
      const response = await getFactoryRuns();
      return response.data;
    },
  });

  if (runId) {
    return <RunDetail runId={runId} />;
  }

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
            run={run}
            onClick={() => {
              const params = new URLSearchParams(searchParams);
              params.set("run", run.run_id);
              setSearchParams(params);
            }}
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
