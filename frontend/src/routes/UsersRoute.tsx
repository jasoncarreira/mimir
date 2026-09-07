import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import React from "react";

import { issueUserKey, listUsers, revokeUserKey } from "../api/admin-users";
import type { AdminUser } from "../api/generated/contracts";
import {
  Badge,
  Button,
  CodeBlock,
  DataTable,
  EmptyState,
  ErrorState,
  LoadingState,
  Panel,
  TextInput
} from "../ui";

// Admin Users view (github #563). Lists identities (never their keys), and
// creates/rotates/revokes per-user web keys. A freshly minted key is shown
// EXACTLY once for out-of-band hand-off; only its hash is stored server-side.
// Rendered inside the consolidated Admin surface's "Users" sub-tab
// (AdminRoute owns the page header).
export function UsersView() {
  const queryClient = useQueryClient();
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["admin-users"],
    queryFn: async () => (await listUsers()).data
  });

  const [canonical, setCanonical] = React.useState("");
  const [label, setLabel] = React.useState("");
  const [role, setRole] = React.useState<"user" | "admin">("user");
  const [mintedKey, setMintedKey] = React.useState<{ canonical: string; key: string } | null>(null);
  const [actionError, setActionError] = React.useState<string | null>(null);

  const refresh = () => queryClient.invalidateQueries({ queryKey: ["admin-users"] });
  const fail = (e: unknown) => setActionError(e instanceof Error ? e.message : String(e));

  const issue = useMutation({
    mutationFn: (vars: { canonical: string; role: "user" | "admin" | null; label?: string; rotate?: boolean }) =>
      issueUserKey(vars.canonical, vars.role, { label: vars.label, rotate: vars.rotate ?? false }),
    onSuccess: (res) => {
      setMintedKey({ canonical: res.data.canonical, key: res.data.key });
      setActionError(null);
      void refresh();
    },
    onError: fail
  });

  const revoke = useMutation({
    mutationFn: (target: { canonical: string; label?: string }) => revokeUserKey(target.canonical, target.label),
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

      <Panel title="Create user / add key" subtitle="Sets the role and adds a key without invalidating existing keys. Leave the label blank for an automatic label.">
        <form
          className="auth-form"
          onSubmit={(event) => {
            event.preventDefault();
            const target = canonical.trim();
            if (!target) return;
            issue.mutate({ canonical: target, role, label: label.trim() || undefined });
            setCanonical("");
            setLabel("");
          }}
        >
          <TextInput
            aria-label="Canonical id"
            placeholder="canonical id (e.g. alice)"
            value={canonical}
            onChange={(event) => setCanonical(event.target.value)}
          />
          <TextInput
            aria-label="Key label"
            placeholder="key label (e.g. laptop)"
            value={label}
            onChange={(event) => setLabel(event.target.value)}
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
            Add key
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
              { key: "webkey", header: "Web keys" },
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
                <div>
                  {user.web_keys.map((key) => (
                    <div key={key.label} className="route-state-form__actions">
                      <span>{key.label}</span>
                      <Badge tone="success">present</Badge>
                      <Button
                        disabled={busy}
                        aria-label={`Revoke ${key.label} for ${user.canonical}`}
                        onClick={() => revoke.mutate({ canonical: user.canonical, label: key.label })}
                      >
                        Revoke key
                      </Button>
                    </div>
                  ))}
                  {!user.has_web_key ? <Badge tone="neutral">none</Badge> : null}
                </div>
              ),
              actions: (
                <span className="route-state-form__actions">
                  <Button
                    disabled={busy}
                    onClick={() => issue.mutate({ canonical: user.canonical, role: null, rotate: true })}
                  >
                    Rotate all keys
                  </Button>
                  <Button
                    disabled={busy || !user.has_web_key}
                    onClick={() => revoke.mutate({ canonical: user.canonical })}
                  >
                    Revoke all keys
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
