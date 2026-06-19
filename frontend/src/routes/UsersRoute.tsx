import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import React from "react";

import { issueUserKey, listUsers, revokeUserKey } from "../api/admin-users";
import type { AdminUser } from "../api/generated/contracts";
import {
  Badge,
  Button,
  CodeBlock,
  DashboardHeader,
  DataTable,
  EmptyState,
  ErrorState,
  LoadingState,
  Panel,
  TextInput
} from "../ui";

// Admin Users page (github #563). Lists identities (never their keys), and
// creates/rotates/revokes per-user web keys. A freshly minted key is shown
// EXACTLY once for out-of-band hand-off; only its hash is stored server-side.
export function UsersRoute() {
  const queryClient = useQueryClient();
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["admin-users"],
    queryFn: async () => (await listUsers()).data
  });

  const [canonical, setCanonical] = React.useState("");
  const [role, setRole] = React.useState<"user" | "admin">("user");
  const [mintedKey, setMintedKey] = React.useState<{ canonical: string; key: string } | null>(null);
  const [actionError, setActionError] = React.useState<string | null>(null);

  const refresh = () => queryClient.invalidateQueries({ queryKey: ["admin-users"] });
  const fail = (e: unknown) => setActionError(e instanceof Error ? e.message : String(e));

  const issue = useMutation({
    mutationFn: (vars: { canonical: string; role: "user" | "admin" | null }) =>
      issueUserKey(vars.canonical, vars.role),
    onSuccess: (res) => {
      setMintedKey({ canonical: res.data.canonical, key: res.data.key });
      setActionError(null);
      void refresh();
    },
    onError: fail
  });

  const revoke = useMutation({
    mutationFn: (target: string) => revokeUserKey(target),
    onSuccess: () => {
      setActionError(null);
      void refresh();
    },
    onError: fail
  });

  const users = data?.users ?? [];
  const busy = issue.isPending || revoke.isPending;

  return (
    <>
      <DashboardHeader eyebrow="Admin" title="Users">
        <p className="app-copy">
          Per-user web keys and roles. A key is shown <strong>once</strong> at creation —
          copy it and hand it to the user out of band; only a hash is stored.
        </p>
      </DashboardHeader>

      {mintedKey ? (
        <Panel
          actions={<Button onClick={() => setMintedKey(null)}>Dismiss</Button>}
          title={`New key for ${mintedKey.canonical}`}
          subtitle="Copy this now — it is shown once and cannot be retrieved again."
        >
          <CodeBlock code={mintedKey.key} title="Web API key (X-API-Key)" />
          <Button
            variant="primary"
            onClick={() => { void navigator.clipboard?.writeText(mintedKey.key); }}
          >
            Copy key
          </Button>
        </Panel>
      ) : null}

      <Panel title="Create user / rotate key" subtitle="Sets the role and issues a fresh key.">
        <form
          className="auth-form"
          onSubmit={(event) => {
            event.preventDefault();
            const target = canonical.trim();
            if (!target) return;
            issue.mutate({ canonical: target, role });
            setCanonical("");
          }}
        >
          <TextInput
            aria-label="Canonical id"
            placeholder="canonical id (e.g. alice)"
            value={canonical}
            onChange={(event) => setCanonical(event.target.value)}
          />
          <select
            aria-label="Role"
            className="ui-input"
            value={role}
            onChange={(event) => setRole(event.target.value === "admin" ? "admin" : "user")}
          >
            <option value="user">user</option>
            <option value="admin">admin</option>
          </select>
          <Button type="submit" variant="primary" disabled={busy || !canonical.trim()}>
            Create / rotate key
          </Button>
        </form>
      </Panel>

      {actionError ? <ErrorState title="Action failed">{actionError}</ErrorState> : null}

      <Panel title="Users">
        {isLoading ? <LoadingState label="Loading users" /> : null}
        {isError ? (
          <ErrorState title="Failed to load users">
            {error instanceof Error ? error.message : String(error)}
          </ErrorState>
        ) : null}
        {!isLoading && !isError && users.length === 0 ? (
          <EmptyState title="No users defined yet" />
        ) : null}
        {!isLoading && !isError && users.length > 0 ? (
          <DataTable
            caption="Identities"
            columns={[
              { key: "user", header: "User" },
              { key: "roles", header: "Roles" },
              { key: "webkey", header: "Web key" },
              { key: "actions", header: "Actions" }
            ]}
            rows={users.map((user: AdminUser) => ({
              user: (
                <span>
                  <code>{user.canonical}</code>
                  {user.display_name ? ` — ${user.display_name}` : ""}
                </span>
              ),
              roles: user.roles.length ? (
                <span>
                  {user.roles.map((r) => (
                    <Badge key={r} tone={r === "admin" ? "warning" : "neutral"}>{r}</Badge>
                  ))}
                </span>
              ) : (
                <Badge tone="neutral">none</Badge>
              ),
              webkey: (
                <Badge tone={user.has_web_key ? "success" : "neutral"}>
                  {user.has_web_key ? "set" : "none"}
                </Badge>
              ),
              actions: (
                <span className="route-state-form__actions">
                  <Button
                    disabled={busy}
                    onClick={() => issue.mutate({ canonical: user.canonical, role: null })}
                  >
                    Rotate key
                  </Button>
                  <Button
                    disabled={busy || !user.has_web_key}
                    onClick={() => revoke.mutate(user.canonical)}
                  >
                    Revoke
                  </Button>
                </span>
              )
            }))}
          />
        ) : null}
      </Panel>
    </>
  );
}
