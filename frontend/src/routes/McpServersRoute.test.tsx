// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { McpServersView } from "./McpServersRoute";

const { api } = vi.hoisted(() => ({ api: {
  listMCPServers: vi.fn(), saveMCPServer: vi.fn(), removeMCPServer: vi.fn(), saveMCPToolPolicy: vi.fn()
} }));
vi.mock("../api/admin-mcp", () => api);

const envelope = (data: unknown) => ({ ok: true, version: "v1", data });
const tool = {
  tool_id: "tool-1", server_config_id: "server-1", original_tool_name: "search",
  display_name: "mcp_docs_search", config_digest: "config-a", schema_digest: "schema-a",
  classification: "", result_integrity: "untrusted", argument_egress: "taint_gated",
  policy_version: "", is_tombstoned: false
};
const server = {
  server_config_id: "server-1", name: "docs", transport: "stdio", command: "docs-mcp",
  args: [], env: {}, policy_version: "ui-v1", tools: [tool]
};

function renderView() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><McpServersView /></QueryClientProvider>);
}

afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe("McpServersView", () => {
  it("lists servers and opens tool policy editors in the edit modal", async () => {
    api.listMCPServers.mockResolvedValue(envelope({ restart_required: true, servers: [server] }));
    api.saveMCPToolPolicy.mockResolvedValue(envelope({ tool, restart_required: true }));
    renderView();
    // Server shows in the list; its tools are NOT surfaced until the modal opens.
    expect(await screen.findByText("docs")).toBeTruthy();
    expect(screen.queryByText("search")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(await screen.findByText("search")).toBeTruthy();
    expect((screen.getByLabelText("Authorization tier") as HTMLSelectElement).value).toBe("admin_required");

    fireEvent.change(screen.getByLabelText("Result integrity"), { target: { value: "trusted" } });
    expect((screen.getByRole("button", { name: "Save policy" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByText(/I understand this trusts external output/i));
    fireEvent.click(screen.getByRole("button", { name: "Save policy" }));
    await waitFor(() => expect(api.saveMCPToolPolicy).toHaveBeenCalled());
  });

  it("adds a stdio server via the modal and parses args and environment", async () => {
    api.listMCPServers.mockResolvedValue(envelope({ restart_required: true, servers: [] }));
    api.saveMCPServer.mockResolvedValue(envelope({ restart_required: true, servers: [] }));
    renderView();
    await screen.findByText("No MCP servers configured");
    fireEvent.click(screen.getByRole("button", { name: "Add server" }));
    fireEvent.change(screen.getByLabelText("Server name"), { target: { value: "docs" } });
    fireEvent.change(screen.getByLabelText("Command"), { target: { value: "uvx" } });
    fireEvent.change(screen.getByLabelText("Arguments"), { target: { value: "server\n--quiet" } });
    fireEvent.change(screen.getByLabelText("Environment"), { target: { value: "TOKEN=${TOKEN}" } });
    fireEvent.click(screen.getByRole("button", { name: "Add and discover" }));
    await waitFor(() => expect(api.saveMCPServer).toHaveBeenCalledWith({
      name: "docs", command: "uvx", transport: "stdio", args: ["server", "--quiet"], env: { TOKEN: "${TOKEN}" }
    }, undefined));
  });

  it("renders transport as a static field, not an interactive dropdown", async () => {
    api.listMCPServers.mockResolvedValue(envelope({ restart_required: true, servers: [] }));
    renderView();
    await screen.findByText("No MCP servers configured");
    fireEvent.click(screen.getByRole("button", { name: "Add server" }));
    // No labelled form control for Transport (it is fixed to stdio in v1)…
    expect(screen.queryByLabelText("Transport")).toBeNull();
    // …but the value is still shown to the operator.
    expect(screen.getByText("Transport")).toBeTruthy();
    expect(screen.getByText("stdio")).toBeTruthy();
  });
});
