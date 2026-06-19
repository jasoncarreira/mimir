// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { UsersRoute } from "./UsersRoute";

const { api } = vi.hoisted(() => ({
  api: { listUsers: vi.fn(), issueUserKey: vi.fn(), revokeUserKey: vi.fn() }
}));

vi.mock("../api/admin-users", () => ({
  listUsers: api.listUsers,
  issueUserKey: api.issueUserKey,
  revokeUserKey: api.revokeUserKey
}));

function renderUsers() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <UsersRoute />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

const envelope = (data: unknown) => ({ ok: true, version: "v1", data });

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("UsersRoute (#563)", () => {
  it("lists users with roles + key status", async () => {
    api.listUsers.mockResolvedValue(
      envelope({
        users: [
          { canonical: "alice", display_name: "Alice", roles: ["user"], is_admin: false, has_web_key: true },
          { canonical: "ops", display_name: null, roles: ["user", "admin"], is_admin: true, has_web_key: false }
        ]
      })
    );
    renderUsers();
    expect(await screen.findByText("alice")).toBeTruthy();
    expect(screen.getByText("ops")).toBeTruthy();
    expect(screen.getByText("set")).toBeTruthy(); // alice has a key
  });

  it("mints a key and shows it exactly once (not before)", async () => {
    api.listUsers.mockResolvedValue(envelope({ users: [] }));
    api.issueUserKey.mockResolvedValue(envelope({ canonical: "bob", key: "sk-minted-once-123" }));
    renderUsers();
    await screen.findByText(/No users defined/i);
    expect(screen.queryByText("sk-minted-once-123")).toBeNull(); // not shown before mint

    fireEvent.change(screen.getByLabelText("Canonical id"), { target: { value: "bob" } });
    fireEvent.click(screen.getByRole("button", { name: /Create \/ rotate key/i }));

    await waitFor(() => expect(api.issueUserKey).toHaveBeenCalledWith("bob", "user"));
    expect(await screen.findByText("sk-minted-once-123")).toBeTruthy(); // shown once after mint
  });

  it("rotates with role:null and revokes by canonical", async () => {
    api.listUsers.mockResolvedValue(
      envelope({
        users: [{ canonical: "alice", display_name: null, roles: ["user"], is_admin: false, has_web_key: true }]
      })
    );
    api.issueUserKey.mockResolvedValue(envelope({ canonical: "alice", key: "rotated-key" }));
    api.revokeUserKey.mockResolvedValue(envelope({ canonical: "alice", revoked: true }));
    renderUsers();
    await screen.findByText("alice");

    fireEvent.click(screen.getByRole("button", { name: "Rotate key" }));
    await waitFor(() => expect(api.issueUserKey).toHaveBeenCalledWith("alice", null));

    fireEvent.click(screen.getByRole("button", { name: /Revoke/i }));
    await waitFor(() => expect(api.revokeUserKey).toHaveBeenCalledWith("alice"));
  });
});
