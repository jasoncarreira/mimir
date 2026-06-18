import { useQuery } from "@tanstack/react-query";
import React from "react";
import { getAdminConfig } from "../api";
import type { AdminConfigData, AdminConfigEnvItem } from "../api/generated/contracts";
import {
  Badge,
  CodeBlock,
  DashboardHeader,
  DataTable,
  EmptyState,
  ErrorState,
  LoadingState,
  Panel
} from "../ui";

function display(value: unknown) {
  if (value === null || value === undefined || value === "") return "n/a";
  if (typeof value === "boolean") return value ? "yes" : "no";
  return String(value);
}

function redactionTone(item: AdminConfigEnvItem) {
  if (!item.present) return "neutral";
  return item.secret ? "warning" : "success";
}

function EnvTable({ rows }: { rows: AdminConfigEnvItem[] }) {
  if (!rows.length) return <EmptyState title="No environment entries reported" />;
  return (
    <DataTable
      caption="Redacted environment inventory"
      columns={[
        { key: "name", header: "Name" },
        { key: "category", header: "Category" },
        { key: "present", header: "Present" },
        { key: "value", header: "Value" }
      ]}
      rows={rows.map((item) => ({
        name: <code>{item.name}</code>,
        category: item.category,
        present: <Badge tone={redactionTone(item)}>{item.present ? "present" : "unset"}</Badge>,
        value: item.secret ? "[REDACTED]" : display(item.value)
      }))}
    />
  );
}

function ModelSummary({ data }: { data: AdminConfigData }) {
  const model = data.model;
  return (
    <Panel
      actions={<Badge tone="info">{model.provider}</Badge>}
      title="Effective Model"
      subtitle={model.model_spec}
    >
      <dl className="facts-grid">
        <div><dt>Provider prefix</dt><dd>{display(model.provider_prefix)}</dd></div>
        <div><dt>Model</dt><dd>{display(model.model)}</dd></div>
        <div><dt>Context window</dt><dd>{model.context_window}</dd></div>
        <div><dt>Base URL override</dt><dd>{model.anthropic_base_url_present ? "present" : "unset"}</dd></div>
        <div><dt>Billing mode</dt><dd>{display(model.resource_window.billing_mode)}</dd></div>
        <div><dt>Usage block</dt><dd>{display(model.resource_window.usage_block_enabled)}</dd></div>
        <div><dt>Rate-limit capture</dt><dd>{display(model.resource_window.capture_rate_limits)}</dd></div>
        <div><dt>Max output tokens</dt><dd>{display(model.resource_window.max_output_tokens)}</dd></div>
      </dl>
    </Panel>
  );
}

function SchemaPanel({ data }: { data: AdminConfigData }) {
  return (
    <Panel title="Config Schema" subtitle="Typed backend sections. All fields are read-only in this v1 surface.">
      <DataTable
        columns={[
          { key: "section", header: "Section" },
          { key: "field", header: "Field" },
          { key: "type", header: "Type" },
          { key: "mutable", header: "Mutable" }
        ]}
        rows={data.schema_sections.flatMap((section) =>
          section.fields.map((field) => ({
            section: section.label,
            field: <code>{field.name}</code>,
            type: field.type,
            mutable: field.mutable ? "yes" : "no"
          }))
        )}
      />
    </Panel>
  );
}

function SchedulesPanel({ data }: { data: AdminConfigData }) {
  return (
    <Panel title="Schedules and Pollers" subtitle="Configured entries only; controls are intentionally omitted.">
      <div className="admin-config__split">
        <div>
          <h3>Schedules</h3>
          {data.schedules.length ? (
            <DataTable
              columns={[
                { key: "name", header: "Name" },
                { key: "kind", header: "Kind" },
                { key: "cron", header: "Cron" },
                { key: "priority", header: "Priority" }
              ]}
              rows={data.schedules.map((item) => ({
                name: <code>{item.name}</code>,
                kind: item.kind,
                cron: display(item.cron ?? item.time_of_day),
                priority: display(item.priority)
              }))}
            />
          ) : (
            <EmptyState title="No scheduler.yaml entries" />
          )}
        </div>
        <div>
          <h3>Pollers</h3>
          {data.pollers.length ? (
            <DataTable
              columns={[
                { key: "name", header: "Name" },
                { key: "cron", header: "Cron" },
                { key: "priority", header: "Priority" }
              ]}
              rows={data.pollers.map((item) => ({
                name: <code>{item.name}</code>,
                cron: item.cron,
                priority: item.priority
              }))}
            />
          ) : (
            <EmptyState title="No registered pollers" />
          )}
        </div>
      </div>
    </Panel>
  );
}

export function AdminConfigRoute() {
  const { data, error, isError, isLoading } = useQuery({
    queryKey: ["admin-config"],
    queryFn: async () => {
      const envelope = await getAdminConfig();
      return envelope.data;
    }
  });

  return (
    <>
      <DashboardHeader eyebrow="Admin" title="Config, Model, and Env">
        <p>Read-only runtime inspection with redacted secret visibility.</p>
      </DashboardHeader>

      {isLoading ? <LoadingState label="Loading admin config" /> : null}
      {isError ? (
        <ErrorState title="Admin config unavailable">
          {error instanceof Error ? error.message : String(error)}
        </ErrorState>
      ) : null}

      {data ? (
        <div className="ops-panel-stack admin-config">
          <ModelSummary data={data} />
          <Panel
            actions={<Badge tone={data.mutation_policy.reveal_secret_values ? "danger" : "success"}>read-only</Badge>}
            title="Mutation Policy"
            subtitle="Secret reveal and edit endpoints are omitted in v1."
          >
            <dl className="facts-grid facts-grid--compact">
              <div><dt>Mode</dt><dd>{data.mutation_policy.mode}</dd></div>
              <div><dt>Reveal path</dt><dd>{display(data.mutation_policy.reveal_path)}</dd></div>
              <div><dt>Edit path</dt><dd>{display(data.mutation_policy.edit_path)}</dd></div>
              <div><dt>Rate limited</dt><dd>{display(data.mutation_policy.rate_limited)}</dd></div>
            </dl>
          </Panel>
          <SchemaPanel data={data} />
          <SchedulesPanel data={data} />
          <Panel title="Environment and API Keys" subtitle="Secret-like names show presence only.">
            <EnvTable rows={data.env} />
          </Panel>
          <Panel title="Raw Config" subtitle={`Generated ${data.generated_at}. Secret-bearing fields are redacted.`}>
            <CodeBlock code={JSON.stringify(data.raw_config, null, 2)} language="json" />
          </Panel>
        </div>
      ) : null}
    </>
  );
}
