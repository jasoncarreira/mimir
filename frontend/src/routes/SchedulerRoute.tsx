import { useQuery } from "@tanstack/react-query";
import React from "react";
import { Link, useSearchParams } from "react-router-dom";
import { getSchedulerDashboard } from "../api";
import { drilldownHref } from "../routeState";
import {
  Badge,
  Button,
  DataTable,
  EmptyState,
  ErrorState,
  LoadingState,
  Panel,
  TextInput
} from "../ui";
import {
  dueWindowOptions,
  formatDateTime,
  runStateLabel,
  runStateTone,
  safeSchedulerDashboardData,
  type SafeSchedulerDashboardData
} from "./schedulerViewModel";

function RunTable({
  title,
  empty,
  rows
}: {
  title: string;
  empty: string;
  rows: SafeSchedulerDashboardData["schedules"];
}) {
  return (
    <Panel title={title}>
      {rows.length ? (
        <DataTable
          columns={[
            { key: "name", header: "Name" },
            { key: "next", header: "Next" },
            { key: "last", header: "Last" },
            { key: "priority", header: "Priority" },
            { key: "channel", header: "Channel" },
            { key: "source", header: "Prompt source" },
            { key: "status", header: "Status" },
            { key: "detail", header: "Result / suppression" },
            { key: "trace", header: "Trace" },
            { key: "config", header: "Config" }
          ]}
          rows={rows.map((row) => ({
            name: <code>{row.name}</code>,
            next: formatDateTime(row.next_run_at),
            last: formatDateTime(row.last_run_at),
            priority: <Badge tone={row.priority === "high" ? "warning" : "info"}>{row.priority}</Badge>,
            channel: row.channel ? <code>{row.channel}</code> : "n/a",
            source: row.prompt_source,
            status: <Badge tone={runStateTone(row)}>{runStateLabel(row)}</Badge>,
            detail: row.recent_error || row.suppression_reason || row.recent_result || "",
            trace: (
              <>
                <Link to={drilldownHref("/turns", {
                  job: row.name,
                  filter: row.recent_error ? "failure" : row.kind,
                  event: row.kind,
                  channel: row.channel || undefined,
                  q: row.name
                })}>Turns</Link>{" "}
                <Link to={drilldownHref("/ops", { tab: "scheduler", job: row.name, filter: row.kind })}>Ops</Link>
              </>
            ),
            config: row.kind === "poller"
              ? (row.pass_env?.length ? <code>{row.pass_env.join(", ")}</code> : "")
              : (Object.keys(row.config || {}).length ? <code>redacted config</code> : "")
          }))}
        />
      ) : (
        <EmptyState title={empty} />
      )}
    </Panel>
  );
}

function DueWindowFilter({ value }: { value: string }) {
  const [, setSearchParams] = useSearchParams();
  return (
    <form
      className="route-state-form route-state-form--inline"
      onSubmit={(event) => {
        event.preventDefault();
        const form = new FormData(event.currentTarget);
        const dueWindow = String(form.get("due_window") || "all");
        setSearchParams((params) => {
          if (dueWindow === "all") params.delete("due_window");
          else params.set("due_window", dueWindow);
          return params;
        }, { replace: true });
      }}
    >
      <label>
        <span>Due window</span>
        <select className="ui-input" defaultValue={value} name="due_window">
          {dueWindowOptions.map((option) => (
            <option key={option.value} value={option.value}>{option.label}</option>
          ))}
        </select>
      </label>
      <Button type="submit" variant="primary">Apply</Button>
    </form>
  );
}

function CommitmentsPanel({ data }: { data: SafeSchedulerDashboardData }) {
  return (
    <Panel
      title="Commitments"
      subtitle="Active commitments filtered by due window."
      actions={<DueWindowFilter value={data.due_window} />}
    >
      {data.commitments.length ? (
        <DataTable
          columns={[
            { key: "id", header: "ID" },
            { key: "due", header: "Due" },
            { key: "status", header: "Status" },
            { key: "channel", header: "Channel" },
            { key: "text", header: "Commitment" },
            { key: "snooze", header: "Snoozes" }
          ]}
          rows={data.commitments.map((row) => ({
            id: <code>{row.id}</code>,
            due: row.due_window_start ? formatDateTime(row.due_window_start) : row.due_window_hint || row.due_bucket,
            status: <Badge tone={row.status === "snoozed" ? "warning" : "info"}>{row.status}</Badge>,
            channel: row.channel ? <code>{row.channel}</code> : "unbound",
            text: row.text,
            snooze: row.snooze_count.toLocaleString()
          }))}
        />
      ) : (
        <EmptyState title="No active commitments in this due window" />
      )}
    </Panel>
  );
}

function DeferredActionsPanel({ data }: { data: SafeSchedulerDashboardData }) {
  return (
    <Panel
      title="Actions"
      actions={<Badge tone={data.actions.mutations_enabled ? "warning" : "info"}>{data.actions.mutations_enabled ? "enabled" : "read-only"}</Badge>}
    >
      <p className="app-copy">{data.actions.policy}</p>
      <DataTable
        columns={[
          { key: "action", header: "Action" },
          { key: "state", header: "State" }
        ]}
        rows={data.actions.deferred.map((action) => ({
          action,
          state: <Badge tone="info">deferred</Badge>
        }))}
      />
    </Panel>
  );
}

export function SchedulerRoute() {
  const [searchParams] = useSearchParams();
  const dueWindow = searchParams.get("due_window") || "all";
  const { data, error, isError, isLoading } = useQuery({
    queryKey: ["scheduler-dashboard", dueWindow],
    queryFn: async () => {
      const envelope = await getSchedulerDashboard({ due_window: dueWindow }, { cache: "no-store" });
      return safeSchedulerDashboardData(envelope.data);
    },
    refetchInterval: 30_000
  });

  return (
    <>
      <header className="ui-header">
        <p className="ui-eyebrow">Operations</p>
        <h1>Scheduler</h1>
        <div className="ui-header__body">
          <TextInput aria-label="Generated at" readOnly value={data ? `Generated ${formatDateTime(data.generated_at)}` : "Loading"} />
        </div>
      </header>
      {isLoading ? <LoadingState label="Loading scheduler dashboard" /> : null}
      {isError ? (
        <ErrorState title="Scheduler dashboard failed">
          {error instanceof Error ? error.message : String(error)}
        </ErrorState>
      ) : null}
      {data ? (
        <div className="ops-panel-stack">
          {!data.available ? (
            <ErrorState title="Scheduler unavailable">The server did not attach a scheduler instance to this web app.</ErrorState>
          ) : null}
          <RunTable title="Schedules" empty="No configured schedules" rows={data.schedules} />
          <RunTable title="Pollers" empty="No registered pollers" rows={data.pollers} />
          <CommitmentsPanel data={data} />
          <DeferredActionsPanel data={data} />
        </div>
      ) : null}
    </>
  );
}
