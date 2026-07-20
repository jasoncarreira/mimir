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
import { Badge, Button, DashboardHeader, EmptyState, ErrorState, LoadingState, Panel, TextInput } from "../ui";

const CLOSED_POLICY = {
  classification: "admin_required" as MCPAuthorizationTier,
  result_integrity: "untrusted" as MCPResultIntegrity,
  argument_egress: "taint_gated" as MCPArgumentEgress
};

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
  onSave: (tool: MCPToolRecord, policy: typeof CLOSED_POLICY) => void;
}) {
  const [policy, setPolicy] = React.useState<typeof CLOSED_POLICY>({
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

export function McpServersRoute() {
  const client = useQueryClient();
  const query = useQuery({ queryKey: ["admin-mcp-servers"], queryFn: async () => (await listMCPServers()).data });
  const [editing, setEditing] = React.useState<MCPServerRecord | null>(null);
  const [name, setName] = React.useState("");
  const [command, setCommand] = React.useState("");
  const [args, setArgs] = React.useState("");
  const [env, setEnv] = React.useState("");
  const [actionError, setActionError] = React.useState<string | null>(null);
  const refresh = () => client.invalidateQueries({ queryKey: ["admin-mcp-servers"] });
  const fail = (error: unknown) => setActionError(error instanceof Error ? error.message : String(error));
  const saved = () => { setActionError(null); setEditing(null); setName(""); setCommand(""); setArgs(""); setEnv(""); void refresh(); };
  const saveServer = useMutation({ mutationFn: (input: MCPServerInput) => saveMCPServer(input, editing?.server_config_id), onSuccess: saved, onError: fail });
  const removeServer = useMutation({ mutationFn: (serverId: string) => removeMCPServer(serverId), onSuccess: saved, onError: fail });
  const savePolicy = useMutation({ mutationFn: ({ tool, policy }: { tool: MCPToolRecord; policy: typeof CLOSED_POLICY }) => saveMCPToolPolicy(tool, policy), onSuccess: () => { setActionError(null); void refresh(); }, onError: fail });
  const busy = saveServer.isPending || removeServer.isPending || savePolicy.isPending;

  function beginEdit(server: MCPServerRecord) {
    setEditing(server); setName(server.name); setCommand(server.command);
    setArgs(server.args.join("\n")); setEnv(Object.entries(server.env).map(([key, value]) => `${key}=${value}`).join("\n"));
  }

  return (
    <div className="mcp-route">
      <DashboardHeader eyebrow="Admin" title="MCP servers">
        <p className="app-copy">Configure stdio servers and bind every discovered tool to an explicit authorization and IFC posture. Changes apply after restart.</p>
      </DashboardHeader>
      <Panel title={editing ? `Edit ${editing.name}` : "Add server"} subtitle="Tools are enumerated immediately. New and changed tools remain admin-only, untrusted, and taint-gated until saved.">
        <form className="mcp-server-form" onSubmit={(event) => {
          event.preventDefault();
          try { saveServer.mutate({ name: name.trim(), command: command.trim(), transport: "stdio", args: args.split("\n").map((item) => item.trim()).filter(Boolean), env: parseEnv(env) }); }
          catch (error) { fail(error); }
        }}>
          <label>Name<TextInput aria-label="Server name" required value={name} onChange={(event) => setName(event.target.value)} /></label>
          <label>Transport<select aria-label="Transport" className="ui-input" disabled value="stdio"><option value="stdio">stdio</option></select></label>
          <label>Command<TextInput aria-label="Command" required value={command} onChange={(event) => setCommand(event.target.value)} /></label>
          <label>Arguments, one per line<textarea aria-label="Arguments" className="ui-input" rows={4} value={args} onChange={(event) => setArgs(event.target.value)} /></label>
          <label>Environment, KEY=value<textarea aria-label="Environment" className="ui-input" rows={4} value={env} onChange={(event) => setEnv(event.target.value)} /></label>
          <div className="route-state-form__actions"><Button disabled={busy || !name.trim() || !command.trim()} type="submit" variant="primary">{editing ? "Rediscover and save" : "Add and discover"}</Button>{editing ? <Button onClick={() => saved()} type="button">Cancel</Button> : null}</div>
        </form>
      </Panel>
      {actionError ? <ErrorState title="Action failed">{actionError}</ErrorState> : null}
      {query.isLoading ? <LoadingState label="Loading MCP servers" /> : null}
      {query.isError ? <ErrorState title="Failed to load MCP servers">{query.error instanceof Error ? query.error.message : String(query.error)}</ErrorState> : null}
      {!query.isLoading && !query.isError && !query.data?.servers.length ? <EmptyState title="No MCP servers configured" /> : null}
      {query.data?.servers.map((server) => (
        <Panel key={server.server_config_id} title={server.name} subtitle={`${server.command} ${server.args.join(" ")}`} actions={<><Badge tone="warning">restart required</Badge><Button disabled={busy} onClick={() => beginEdit(server)}>Edit</Button><Button disabled={busy} onClick={() => { if (window.confirm(`Remove ${server.name}? Its tools will be tombstoned.`)) removeServer.mutate(server.server_config_id); }}>Remove</Button></>}>
          <div className="mcp-server-meta"><span><strong>Transport</strong> stdio</span><span><strong>Tools</strong> {server.tools.length}</span><span><strong>Policy</strong> {server.policy_version}</span></div>
          {!server.tools.length ? <EmptyState title="No tools discovered" /> : server.tools.map((tool) => <ToolPolicyEditor busy={busy} key={tool.tool_id} onSave={(selected, policy) => savePolicy.mutate({ tool: selected, policy })} tool={tool} />)}
        </Panel>
      ))}
    </div>
  );
}
