import { apiFetchEnvelope, type ApiClientOptions } from "./http";
import type {
  AdminMCPData,
  ApiSuccessEnvelope,
  MCPArgumentEgress,
  MCPAuthorizationTier,
  MCPResultIntegrity,
  MCPToolRecord
} from "./generated/contracts";

export interface MCPServerInput {
  name: string;
  transport: "stdio";
  command: string;
  args: string[];
  env: Record<string, string>;
}

export function listMCPServers(options?: ApiClientOptions & RequestInit) {
  return apiFetchEnvelope<AdminMCPData>("/api/v1/admin/mcp/servers", {
    cache: "no-store",
    ...options
  });
}

export function saveMCPServer(
  input: MCPServerInput,
  serverId?: string,
  options?: ApiClientOptions & RequestInit
): Promise<ApiSuccessEnvelope<AdminMCPData>> {
  const path = serverId
    ? `/api/v1/admin/mcp/servers/${encodeURIComponent(serverId)}`
    : "/api/v1/admin/mcp/servers";
  return apiFetchEnvelope<AdminMCPData>(path, {
    method: serverId ? "PUT" : "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
    ...options
  });
}

export function removeMCPServer(serverId: string, options?: ApiClientOptions & RequestInit) {
  return apiFetchEnvelope<{ removed: boolean; restart_required: boolean }>(
    `/api/v1/admin/mcp/servers/${encodeURIComponent(serverId)}`,
    { method: "DELETE", ...options }
  );
}

export function saveMCPToolPolicy(
  tool: MCPToolRecord,
  policy: {
    classification: MCPAuthorizationTier;
    result_integrity: MCPResultIntegrity;
    argument_egress: MCPArgumentEgress;
  },
  options?: ApiClientOptions & RequestInit
) {
  return apiFetchEnvelope<{ tool: MCPToolRecord; restart_required: boolean }>(
    `/api/v1/admin/mcp/tools/${encodeURIComponent(tool.tool_id)}/policy`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ...policy,
        config_digest: tool.config_digest,
        schema_digest: tool.schema_digest
      }),
      ...options
    }
  );
}
