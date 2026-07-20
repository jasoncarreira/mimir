import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import React from "react";

import {
  listMCPServers,
  removeMCPServer,
  saveMCPServer,
  saveMCPToolPolicy,
  type MCPServerInput
} from "../api/admin-mcp";
import type {
  MCPArgumentEgress,
  MCPAuthorizationTier,
  MCPResultIntegrity,
  MCPServerRecord,
  MCPToolRecord
} from "../api/generated/contracts";
import {
  Badge,
  Button,
  DataTable,
  Dialog,
  EmptyState,
  ErrorState,
  LoadingState,
  Panel,
  TextInput
} from "../ui";

const CLOSED_POLICY = {
  classification: "admin_required" as MCPAuthorizationTier,
  result_integrity: "untrusted" as MCPResultIntegrity,
  argument_egress: "taint_gated" as MCPArgumentEgress
};

type ToolPolicy = typeof CLOSED_POLICY;

function parseEnv(value: string): Record<string, string> {
  return Object.fromEntries(value.split("\n").map((line) => line.trim()).filter(Boolean).map((line) => {
    const index = line.indexOf("=");
    if (index < 1) throw new Error(`Invalid environment line: ${line}`);
    return [line.slice(0, index).trim(), line.slice(index + 1)];
  }));
}

function ToolPolicyEditor({ tool, busy, onSave }: {
  tool: MCPToolRecord;
  busy: boolean;
  onSave: (tool: MCPToolRecord, policy: ToolPolicy) => void;
}) {
  const [policy, setPolicy] = React.useState<ToolPolicy>({
    classification: tool.classification || CLOSED_POLICY.classification,
    result_integrity: tool.result_integrity,
    argument_egress: tool.argument_egress
  });
  const widening = policy.result_integrity === "trusted" || policy.argument_egress === "allowed";
  const [confirmed, setConfirmed] = React.useState(false);

  return (
    <div className="mcp-tool">
      <div className="mcp-tool__identity">
        <strong>{tool.original_tool_name}</strong>
        <code>{tool.schema_digest}</code>
        {tool.is_tombstoned ? <Badge tone="warning">drifted</Badge> : null}
      </div>
      <label>Authorization tier
        <select className="ui-input" value={policy.classification} onChange={(event) => setPolicy({ ...policy, classification: event.target.value as MCPAuthorizationTier })}>
          <option value="admin_required">admin required</option>
          <option value="resource_scoped">resource scoped</option>
          <option value="open">open</option>
        </select>
      </label>
      <label>Result integrity
        <select className="ui-input" value={policy.result_integrity} onChange={(event) => { setConfirmed(false); setPolicy({ ...policy, result_integrity: event.target.value as MCPResultIntegrity }); }}>
          <option value="untrusted">untrusted</option>
          <option value="trusted">trusted</option>
        </select>
      </label>
      <label>Argument egress
        <select className="ui-input" value={policy.argument_egress} onChange={(event) => { setConfirmed(false); setPolicy({ ...policy, argument_egress: event.target.value as MCPArgumentEgress }); }}>
          <option value="taint_gated">taint gated</option>
          <option value="allowed">allowed</option>
        </select>
      </label>
      {widening ? (
        <label className="mcp-tool__warning">
          <input checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} type="checkbox" />
          I understand this trusts external output or permits tainted arguments to leave Mimir.
        </label>
      ) : null}
      <Button disabled={busy || tool.is_tombstoned || (widening && !confirmed)} onClick={() => onSave(tool, policy)} variant="primary">
        Save policy
      </Button>
    </div>
  );
}

// Add / edit form as a modal. For an existing server the discovered tools and
// their per-tool policy editors are shown below the config fields.
function ServerFormDialog({ server, busy, error, onSubmit, onSavePolicy, onClose }: {
  server: MCPServerRecord | null;
  busy: boolean;
  error: string | null;
  onSubmit: (input: MCPServerInput) => void;
  onSavePolicy: (tool: MCPToolRecord, policy: ToolPolicy) => void;
  onClose: () => void;
}) {
  const editing = server !== null;
  const [name, setName] = React.useState(server?.name ?? "");
  const [command, setCommand] = React.useState(server?.command ?? "");
  const [args, setArgs] = React.useState(server ? server.args.join("\n") : "");
  const [env, setEnv] = React.useState(
    server ? Object.entries(server.env).map(([key, value]) => `${key}=${value}`).join("\n") : ""
  );
  const [localError, setLocalError] = React.useState<string | null>(null);

  return (
    <Dialog open title={editing ? `Edit ${server!.name}` : "Add MCP server"} onClose={onClose}>
      <form
        className="mcp-server-form"
        onSubmit={(event) => {
          event.preventDefault();
          try {
            setLocalError(null);
            onSubmit({
              name: name.trim(),
              command: command.trim(),
              transport: "stdio",
              args: args.split("\n").map((item) => item.trim()).filter(Boolean),
              env: parseEnv(env)
            });
          } catch (err) {
            setLocalError(err instanceof Error ? err.message : String(err));
          }
        }}
      >
        <label>Name<TextInput aria-label="Server name" required value={name} onChange={(event) => setName(event.target.value)} /></label>
        <div className="mcp-field">
          <span className="mcp-field__label">Transport</span>
          <span className="mcp-field__value">stdio</span>
        </div>
        <label>Command<TextInput aria-label="Command" required value={command} onChange={(event) => setCommand(event.target.value)} /></label>
        <label>Arguments, one per line<textarea aria-label="Arguments" className="ui-input" rows={4} value={args} onChange={(event) => setArgs(event.target.value)} /></label>
        <label>Environment, KEY=value<textarea aria-label="Environment" className="ui-input" rows={4} value={env} onChange={(event) => setEnv(event.target.value)} /></label>
        <div className="route-state-form__actions">
          <Button disabled={busy || !name.trim() || !command.trim()} type="submit" variant="primary">{editing ? "Rediscover and save" : "Add and discover"}</Button>
          <Button onClick={onClose} type="button">Cancel</Button>
        </div>
      </form>
      <p className="app-copy">Tools are enumerated immediately. New and changed tools remain admin-only, untrusted, and taint-gated until saved. Changes apply after restart.</p>
      {localError || error ? <ErrorState title="Action failed">{localError || error}</ErrorState> : null}
      {editing ? (
        <div className="mcp-tools">
          <h3>Discovered tools</h3>
          {!server!.tools.length
            ? <EmptyState title="No tools discovered" />
            : server!.tools.map((tool) => <ToolPolicyEditor busy={busy} key={tool.tool_id} onSave={onSavePolicy} tool={tool} />)}
        </div>
      ) : null}
    </Dialog>
  );
}

// Content-only view: rendered inside the consolidated Admin surface's "MCP
// Servers" sub-tab (AdminRoute owns the page header).
export function McpServersView() {
  const client = useQueryClient();
  const query = useQuery({ queryKey: ["admin-mcp-servers"], queryFn: async () => (await listMCPServers()).data });
  const [dialogOpen, setDialogOpen] = React.useState(false);
  const [dialogServerId, setDialogServerId] = React.useState<string | null>(null);
  const [actionError, setActionError] = React.useState<string | null>(null);

  const servers = query.data?.servers ?? [];
  const dialogServer = dialogServerId ? servers.find((server) => server.server_config_id === dialogServerId) ?? null : null;

  const refresh = () => client.invalidateQueries({ queryKey: ["admin-mcp-servers"] });
  const fail = (error: unknown) => setActionError(error instanceof Error ? error.message : String(error));
  const closeDialog = () => { setDialogOpen(false); setDialogServerId(null); setActionError(null); };

  const saveServer = useMutation({
    mutationFn: (input: MCPServerInput) => saveMCPServer(input, dialogServerId ?? undefined),
    onSuccess: () => { setActionError(null); closeDialog(); void refresh(); },
    onError: fail
  });
  const removeServer = useMutation({
    mutationFn: (serverId: string) => removeMCPServer(serverId),
    onSuccess: () => { setActionError(null); void refresh(); },
    onError: fail
  });
  const savePolicy = useMutation({
    mutationFn: ({ tool, policy }: { tool: MCPToolRecord; policy: ToolPolicy }) => saveMCPToolPolicy(tool, policy),
    onSuccess: () => { setActionError(null); void refresh(); },
    onError: fail
  });
  const busy = saveServer.isPending || removeServer.isPending || savePolicy.isPending;

  function openAdd() { setActionError(null); setDialogServerId(null); setDialogOpen(true); }
  function openEdit(server: MCPServerRecord) { setActionError(null); setDialogServerId(server.server_config_id); setDialogOpen(true); }

  return (
    <div className="mcp-route">
      <Panel
        title="Servers"
        subtitle="stdio MCP servers. Each discovered tool is bound to an explicit authorization tier and IFC posture. Changes apply after restart."
        actions={<Button disabled={busy} onClick={openAdd} variant="primary">Add server</Button>}
      >
        {query.isLoading ? <LoadingState label="Loading MCP servers" /> : null}
        {query.isError ? <ErrorState title="Failed to load MCP servers">{query.error instanceof Error ? query.error.message : String(query.error)}</ErrorState> : null}
        {!query.isLoading && !query.isError && !servers.length ? <EmptyState title="No MCP servers configured" /> : null}
        {servers.length ? (
          <DataTable
            caption="Configured MCP servers"
            columns={[
              { key: "name", header: "Name" },
              { key: "command", header: "Command" },
              { key: "transport", header: "Transport" },
              { key: "tools", header: "Tools" },
              { key: "policy", header: "Policy" },
              { key: "actions", header: "Actions" }
            ]}
            rows={servers.map((server) => ({
              name: <strong>{server.name}</strong>,
              command: <code>{[server.command, ...server.args].join(" ")}</code>,
              transport: "stdio",
              tools: server.tools.length,
              policy: server.policy_version,
              actions: (
                <span className="route-state-form__actions">
                  <Button disabled={busy} onClick={() => openEdit(server)}>Edit</Button>
                  <Button disabled={busy} onClick={() => { if (window.confirm(`Remove ${server.name}? Its tools will be tombstoned.`)) removeServer.mutate(server.server_config_id); }}>Remove</Button>
                </span>
              )
            }))}
          />
        ) : null}
      </Panel>
      {actionError && !dialogOpen ? <ErrorState title="Action failed">{actionError}</ErrorState> : null}
      {dialogOpen ? (
        <ServerFormDialog
          busy={busy}
          error={actionError}
          key={dialogServerId ?? "new"}
          onClose={closeDialog}
          onSavePolicy={(tool, policy) => savePolicy.mutate({ tool, policy })}
          onSubmit={(input) => saveServer.mutate(input)}
          server={dialogServer}
        />
      ) : null}
    </div>
  );
}
