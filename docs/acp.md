# ACP client

## Experimental status

**ACP and Hands are experimental in 0.9.0.** Do not depend on them for
multi-client access, filesystem sandboxing, or durable Python state. Current
operator-visible limits are:

- **One client at a time per daemon home.** A second connection is refused with
  `An ACP client is already connected`. A stray `mimir acp` process can hold the
  slot and is a common cause of a client reporting a launch failure. Close the
  previous client and stop its leftover proxy before retrying; do not start
  another proxy as a connectivity test while a client is attached.
- **File confinement is lexical, not a sandbox.** `hands_read` and `hands_edit`
  reject paths outside the session cwd, but follow in-cwd symlinks even when
  their targets are outside it. Select a trusted project directory and inspect
  its symlinks before granting access.
- **macOS Seatbelt is the only verified execution-confinement backend.** `hands_shell`
  and `hands_python` run under OS-level filesystem confinement by default, and it
  is mandatory wherever a backend is available; a confined child cannot follow a
  symlink out of the approved paths the way `hands_read` and `hands_edit` can.
  macOS Seatbelt uses `sandbox-exec`, which Apple has deprecated. #1597 adds a
  Linux AppArmor backend but does not verify Linux confinement on real hardware.
  With an unavailable backend the tools run only after the operator
  explicitly accepts the unconfined risk, and in that mode the cwd and path-scope
  grants do not protect files. This existing separate operator-consent fallback
  is unchanged; malformed profiles or runtime errors never trigger automatic retry
  without confinement.
- **Python state is temporary.** Closing the client or restarting its proxy loses
  the REPL namespace; loading the daemon transcript does not recover it. Save
  needed results explicitly and rerun initialization after reconnecting.
  Chainlink #1594 tracks project-path-keyed reuse within one live proxy; the
  current implementation is session-keyed, and that planned reuse is not
  persistence across client closure or proxy restart.
- **ACP is admin-only.** Its authenticated admin identity skips non-admin
  protected-read filtering. Validate a read-policy change using a non-admin
  identity on a non-ACP surface, not through an ACP client.
- **ACP enforces authorization.** `mimir/acp/agent.py` deliberately sets
  `enforce=True` as an enforced canary. It is currently the only surface with
  that unconditional override; other surfaces record shadow decisions under
  the shipped default unless global enforcement is explicitly enabled. When
  a refusal appears only in ACP, inspect the authorization decision rather
  than assuming the client is broken. Admin status and client permission do
  not bypass information-flow controls. See the [authorization reference](authorization.md).

## Architecture and daemon

Mimir ACP uses this topology:

```text
stock ACP client
  -> local credential-aware `mimir acp` stdio proxy
  -> local Unix socket OR public-key-authenticated SSH relay
  -> owner-only daemon socket
  -> the one running `mimir run` AgentRuntimeBundle
```

One already-running `mimir run` daemon owns the brain and its `AgentRuntimeBundle`. `mimir acp` is only a local stdio proxy, and `mimir acp relay` is only a credential-blind relay. Neither calls a runtime factory or silently starts Mimir. The proxy never creates a standalone runtime.

ACP is enabled only when `MIMIR_ACP_ENABLED` is explicitly truthy and is disabled when the variable is unset, blank, or false. Unrecognised values fail startup. The daemon listens at `$MIMIR_HOME/.mimir/acp/daemon.sock`. The `.mimir/acp` directory is owned by the daemon UID with mode `0700`; the socket is owner-only with mode `0600`.

stdin and stdout carry UTF-8 JSONL ACP frames only. stdout is reserved before command imports and diagnostics go to stderr. Local socket and relay connection attempts are bounded to 5 seconds. SSH process creation and establishment are bounded to 12 seconds. Once established, a session has no duration limit. Cleanup uses writer drain, close, and abort bounds of 2, 1, and 1 seconds within a 5-second force-close bound, then waits 1 second for SSH, terminates and waits 2 seconds, and kills and waits 1 second.

## Profiles and credentials

Create and manage profiles with these commands, replacing `PROFILE` and absolute paths:

```sh
mimir acp profile add-local PROFILE --home /absolute/server/mimir-home
mimir acp profile add-ssh PROFILE --home /absolute/server/mimir-home \
  --ssh-host host.example --ssh-user mimir --ssh-port 22 \
  --identity-file /absolute/id_ed25519 --known-hosts-file /absolute/known_hosts
mimir acp profile set-timeout PROFILE 60
mimir acp profile list
mimir acp profile remove PROFILE
mimir acp credential add PROFILE
mimir acp credential replace PROFILE
mimir acp credential remove PROFILE
mimir acp credential list
mimir acp --profile PROFILE
```

Profiles contain only non-secret target, home, socket, SSH identity, and execution-timeout data. The timeout is an integer from 1 through 600 seconds and defaults to 60. It applies to locally hosted shell and Python execution in both local and SSH modes. They are stored in `${XDG_CONFIG_HOME:-~/.config}/mimir/acp/profiles.json`. `MIMIR_ACP_PROFILE` may select a non-secret profile name only; it must never contain a key.

On the server, issue the existing named admin web credential:

```sh
mimir identities issue-key --home /absolute/server/mimir-home CANONICAL --admin --label CLIENT_NAME
```

Web keys are multiple-per-identity. Issuing a key is additive and does not
invalidate existing keys; use a distinct label for each client. For an existing
admin identity, `mimir identities issue-key CANONICAL --label CLIENT_NAME`
preserves its roles (select the server home with `--home` when needed).
See [web keys](credentials.md#named-web-keys) for selective revocation.

On the client, run `mimir acp credential add PROFILE`. Enrollment reads the value without echo from a controlling TTY, not stdin. The raw key exists only in the client's native OS credential store under service `mimir.acp`; the server stores only its hash. There is no plaintext or third-party fallback, and enrollment fails if no secure backend exists. If the native store raises after a credential mutation was dispatched, the command exits 3 with `credential-mutation-uncertain`; inspect the native store before retrying. Other validation, profile, secure-store selection, read, and TTY failures exit 1. The raw key must never appear as an SSH password, in `sshpass` or PAM reuse, argv, an environment variable, editor JSON, profile JSON, or registry data. `MIMIR_API_KEY` supplies transport/route authority and is not the ACP principal key.

A stock client sends `authenticate` with only `methodId`. The proxy injects proof only into the protected upstream authenticate request. The daemon resolves it to a non-service admin identity and constructs `AuthContext` server-side. Per-call ACP permission is a second factor and cannot create authority.

Validate enrollment by launching `mimir acp --profile PROFILE` from a stock client and completing its ordinary `authenticate` exchange. There is no `credential validate` command or pre-activation validation protocol.

For an explicit all-key rotation, use this exact order. Unlike additive issuance,
`--rotate-only` invalidates **all** existing web keys for the identity, including
other clients' keys; arrange to replace credentials on every affected client:

1. On the server, run `mimir identities issue-key --home /absolute/server/mimir-home CANONICAL --rotate-only`. It immediately invalidates the old key and prints the new key once.
2. On the client, run `mimir acp credential replace PROFILE` and enter the new value.
3. Reconnect with `mimir acp --profile PROFILE`.

There is an expected outage between steps 1 and 2 and no rollback to the old key. To recover, issue another key and replace the client value again. To retire an identity, first run `mimir identities revoke-key --home /absolute/server/mimir-home CANONICAL` on the server, then `mimir acp credential remove PROFILE` on the client.

## SSH transport

SSH and Mimir provide two independent proofs: an SSH public key or certificate authenticates transport access, while the Mimir web key proves application identity. Never use the Mimir key as an SSH password, with `sshpass`, or for password/PAM reuse.

Test remote noninteractive access with `ssh -T`. The proxy uses batch mode, strict host-key verification, and no forwarding, SSH agent, or TTY. Use an optional dedicated identity with mode `0600`; the known-hosts file must be owner-controlled and not group- or world-writable. Maintain the correct host-key entry. Never use `StrictHostKeyChecking=no`; that literal is prohibited in shell and configuration examples.

For optional defense in depth, restrict a dedicated public key in `authorized_keys`:

```text
restrict,command="mimir-agent acp relay --home /absolute/server/mimir-home" ssh-ed25519 AAAA... dedicated-mimir-acp
```

The forced command fixes one remote home. It is optional defense in depth, not required product behavior. Ensure remote `mimir-agent` is on the account's noninteractive PATH. MOTD, banner, or shell rc output before the relay corrupts JSONL framing and must be removed.

The client account and proxy, the relay/daemon UID, and root are trusted with ACP plaintext. Socket modes do not isolate ACP data from another process running as one of those identities.

### Containerized servers: the sshd add-on

Mimir ships no SSH server. A containerized deployment therefore has no transport an ACP client can reach, and three constraints rule out the alternatives:

- A container has no native OS keystore. `keyring` resolves to `backends.fail.Keyring`, and `mimir acp credential add` refuses any non-native backend, so the proxy must run on the client host, not in the container.
- A Unix socket does not cross a Docker Desktop bind mount on macOS. The daemon socket is simply absent on the host side of the mount, so `profile add-local` has nothing to connect to.
- `mimir acp relay` speaks ACP JSONL on stdin and stdout, which is exactly what an SSH forced command supplies.

Add sshd as an opt-in layer over the built image rather than in the image itself; putting it in the base would widen every deployment for a need only ACP clients have. Layer it in a separate Dockerfile and enable it with a compose overlay, so deploying without the overlay drops sshd again:

```text
ARG BASE=<your-image>:latest
FROM ${BASE}
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-server \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /run/sshd /home/mimir/.ssh \
 && chown mimir:mimir /home/mimir/.ssh && chmod 700 /home/mimir/.ssh
```

Disable every source of pre-relay output in a drop-in `sshd_config.d` file — `PrintMotd no`, `PrintLastLog no`, `Banner none`, `PermitUserRC no` — alongside `PasswordAuthentication no` and `AllowUsers <agent-user>`. Banner or rc output ahead of the relay corrupts JSONL framing.

`PermitUserRC no` matters even though the `restrict` keyword below already disables `~/.ssh/rc`: the agent account owns its home directory and can create that file, so a key authorized with a bare `command=` and no `restrict` would execute it before the relay. Enforce it server-side so the guarantee does not rest on one keyword in `authorized_keys`.

Register sshd as a supervised service rather than overriding the container entrypoint. Under s6, add a longrun whose `run` script ends in `exec /usr/sbin/sshd -D -e`. A compose `command:` override displaces the init that delivers SIGTERM to the agent, which loses the graceful drain.

Publish the port on loopback only (`127.0.0.1:2222:22`), mount `authorized_keys` read-only so the agent cannot append a key granting itself a shell, and pin the key with `restrict,command=` as above. SSH then grants transport and nothing else: the daemon still requires an admin web key over the protocol, and the forced command means a stolen key cannot open a shell. Verify both after deploying — an arbitrary `ssh … 'id'` must produce relay output rather than command output, and a PTY request must be refused.

The client profile then targets the published loopback port:

```text
mimir acp profile add-ssh PROFILE --home /absolute/server/mimir-home \
  --ssh-host 127.0.0.1 --ssh-user <agent-user> --ssh-port 2222 \
  --identity-file ~/.ssh/<dedicated-key> --known-hosts-file <owner-controlled-path>
```

## Stock clients (macOS and Linux)

These configurations support macOS and Linux proxy/client hosts. Windows client support is deferred. Each editor contains only a non-secret profile selector; no raw key or remote SSH command belongs in editor configuration.

### JetBrains AI Assistant

Save this as `~/.jetbrains/acp.json`. The display/id is `mimir`. The providerless configuration disables both integrated MCP sources; ordinary IntelliJ MCP servers are not compatible with Mimir Hands.

```json
{"default_mcp_settings":{"use_idea_mcp":false,"use_custom_mcp":false},"agent_servers":{"mimir":{"command":"/absolute/path/to/uvx","args":["mimir-agent==0.9.0","acp"],"env":{"MIMIR_ACP_PROFILE":"PROFILE"}}}}
```

### Zed

```json
{"agent_servers":{"mimir":{"type":"custom","command":"uvx","args":["mimir-agent==0.9.0","acp"],"env":{"MIMIR_ACP_PROFILE":"PROFILE"}}}}
```

### VS Code

This example uses community extension `formulahendry.acp-client` version `0.2.0`, source commit `e7371659e3ac100db842b419b1361205a193032e`, and its `acp.agents` setting:

```json
{"acp.agents":{"mimir":{"command":"uvx","args":["mimir-agent==0.9.0","acp"],"env":{"MIMIR_ACP_PROFILE":"PROFILE"}}}}
```

As the accepted premise measured 2026-08-09, Microsoft's native VS Code agent system uses AHP, not this community ACP-client integration.

The registry candidate renders the launch shape `uvx mimir-agent==0.9.0 acp`. It is an offline review candidate and does not claim that unpublished version 0.9.0 is already installable. PyPI publication and registry submission remain separately authorized and release-gated after publication and manual smoke testing.

### Registry eligibility

**This candidate is not registry-eligible.** The curated ACP registry requires an agent to advertise at least one authentication method — Agent Auth or Terminal Auth — and this candidate deliberately carries no `authMethods`. Publication to PyPI and manual smoke testing do not unlock submission on their own: an authentication method must be designed, built, and advertised in the manifest before the candidate can be submitted at all. Until then `registry/mimir/agent.json` is a schema-valid rehearsal of the entry, not a submittable one.

### Schema provenance

`registry/schema/agent.schema.json` is vendored from the ACP registry CDN. Its provenance is recorded in `registry/schema/PROVENANCE.json` and pinned by `tests/test_acp_registry.py`, which asserts the vendored bytes hash to the digest recorded there.

Upstream publishes this schema **only** from a moving `latest` path. Versioned CDN paths return 404, and `agent.schema.json` is not committed to the `agent-client-protocol` repository at any tag — that repository's `schema-v*` tags version the wire protocol schema, not the registry entry schema. There is therefore no upstream commit or revision that identifies these bytes, and the recorded digest plus retrieval date is the complete provenance available. Detect upstream drift by re-fetching `source_url` and comparing the digest; a mismatch means the vendored copy and `PROVENANCE.json` must be refreshed together.

## Connections, sessions, and replay

There is one active ACP connection per `MIMIR_HOME`. The daemon refuses a second connection with `An ACP client is already connected`; release the old connection before reconnecting. Only a newly authenticated connection can supersede the prior generation at the authentication boundary; failed or partial authentication cannot evict the active client. This is not permission for a second simultaneous client to take over: the daemon's admission fence runs first. Reconnection creates a new authentication and generation boundary. Session IDs are owner-bound UUIDv4 values, and reconnection resumes them through `session/load`; provider, permission, and MCP request identities are fresh.

The journal has a default seven-day TTL and a 64 MiB limit. Before replay, Mimir revalidates the provider. A load replays every durably prepared `session/update` with its original sequence, including records already sent. Clients must tolerate duplicates. Replay never re-executes effects. Pending requests and frames are not replayed, external effects are not exactly-once, and cancellation does not roll back completed effects.

Transport death cancels and quarantines only that ACP generation. The daemon, web UI, bridges, scheduler, unrelated work, and completed effects remain alive.

## Providers, permissions, and filesystems

When `mcpServers` is missing or empty, the local proxy injects one locally hosted MCP-over-ACP provider named `mimir-hands`. An explicit nonempty provider collection is preserved. The `mimir.hands.v1` profile contains exactly `read`, `edit`, `shell`, `python`, and `request_scope`; it is validated afresh on session new, session load, and provider-list change. Read is prompt-free. Edit, shell, and Python require exact-call operator permission immediately before execution. `allow_session` creates only an in-memory proxy grant for that session and tool, and a tainted call always prompts again unless an admin has acknowledged the current ingest snapshot with `clear_ingest_taint`. Grants never create daemon authority and are revoked on load, disconnect, generation replacement, or proxy exit.

An authenticated, non-service admin on a live user turn can ask Mimir to call
`clear_ingest_taint`. It durably audits and acknowledges only the current ingest
snapshot for the ACP permission prompt, allowing an existing session grant to
apply again. It does **not** clear source labels, declassify data, grant execution
permission, or change sink and durable-memory decisions. Later untrusted active
ingest, including rereading the same source, re-arms the prompt. See
[ingest acknowledgement](authorization.md#ingest-acknowledgement) for eligibility
and audit failure behavior.

**Behavior change in 0.9.0:** the previous `client-file:*` grant permitted
Hands file access to any path on the operator's machine that the client user
could access. The grant now comes from the session cwd. If an old workflow
receives an outside-cwd refusal, open a session rooted at the intended project;
do not rely on a symlink escape as isolation.

Native Mimir tools operate on the daemon host. Mimir Hands operates with the local client's user authority. `hands_read` and `hands_edit` are confined to the session's bound `cwd`: relative paths are normalized against it, and absolute paths outside it (including sibling directories) are refused. This is lexical path confinement only. The daemon cannot resolve symlinks on the client's filesystem; a symlink inside the cwd pointing outside it is still followed. Admins should scope a directory whose content and symlinks they trust. Successful cwd reads retain their source and originating-channel labels but no longer add untrusted active ingest. They do not clear taint from URLs, forge results, messages, or other untrusted sources.

`hands_shell` and `hands_python` use OS-level filesystem confinement by default. It is mandatory when the backend is available. Only an unavailable backend permits the separately approved fallback described below. Their confined child processes start in the session cwd and can access its descendants, exact operator-approved extra paths, and narrowly required runtime paths. The tools take command/code strings rather than paths, so argument checks alone cannot enforce this boundary. Use `hands_request_scope` to request additional paths before execution. Their output remains untrusted active ingest, so a shell result can still cause the next granted shell call to prompt. Operator consent does not declassify that output. Mimir tolerates advertised ACP client `fs` and `terminal` capabilities but never calls them. `additionalDirectories` and arbitrary provider profiles are rejected.

Python keeps one lazy subprocess and in-memory namespace per ACP session. Session load restores the daemon transcript but retires the old worker first, so Python state is never stored in a session or journal and the next call is fresh. Workers retire on load, hosted disconnect, cancellation, daemon-generation replacement, proxy exit, `SIGTERM`, `SIGINT`, `SIGHUP`, or 1,800 seconds of idle time. Shells and Python workers run in owned process groups that are killed and reaped during cleanup. Variables, functions, imports, and loaded data persist only while that worker remains live.

## Troubleshooting

The proxy intentionally reports the generic diagnostic `error: connection-failed`. Confirm the selected profile, then confirm `mimir run` is running with `MIMIR_ACP_ENABLED=true`. As the owner UID, inspect `<MIMIR_HOME>/.mimir/acp`: the directory must be mode `0700`, and `daemon.sock` must be mode `0600`. Start or restart `mimir run` if the daemon is missing or disabled; the proxy will not start it.

For SSH profiles, additionally confirm the remote `mimir-agent` version is 0.9.0, it is on the noninteractive PATH, identity and known-hosts permissions are correct, the host-key entry matches, and remote stdout is banner-free.


### Hands execution scope

Confined Hands shell and Python execution uses the session's approved paths,
initially its cwd and descendants. **Additional directory approvals intentionally
have backend-specific meanings**, even when the permission prompt shows the same
path string. On macOS Seatbelt, additional approved file or directory paths are
literal: approving `/data` approves the directory entry, not `/data/file.txt` or
other descendants. On Linux AppArmor, approving `/data` grants that directory and
all its descendants, never its parent or similarly named siblings. This preserves
the shipped Seatbelt scope while adopting the directory-tree semantics specified
for AppArmor in #1597; it is not an accidental implementation difference. Aligning
the backends, especially widening macOS approvals to include descendants, requires
a separate security decision and is outside this Linux-backend change.
AppArmor's path syntax deliberately supports only ASCII letters, digits,
underscores, dots, slashes, plus signs and hyphens.
macOS Seatbelt (`sandbox-exec`, deprecated by Apple) remains the only verified
backend. #1597 adds Linux AppArmor, not real-hardware verification. The local ACP
proxy host executes Hands, even with a remote daemon; a container cannot verify
that host's confinement. An installed parser or enabled AppArmor LSM alone is
insufficient: an enforcing child transition is required. AppArmor profiles are
loaded only with the current UID's existing authority, without privilege escalation.
The initial policy has fixed system runtime read allowances; nonstandard Python
installations may fail to start and are not retried without confinement.

Live hardware verification, enforcing-vs-complain live checks, and concurrency
naming/cleanup are deferred to the authorized Linux/AppArmor hardware-verification follow-up to #1597.

#### Unavailable-backend risk approval

Confinement is the default and remains mandatory when a backend is available.
Only an unavailable platform or confinement backend can offer unconfined
execution. A malformed profile, profile application error, or child runtime
failure never triggers a downgrade. There is no automatic fallback or
model-controlled opt-in flag.

When the backend is unavailable, the operator must explicitly accept the risk
over `session/request_permission` before execution can start. The warning states
that `hands_shell` and `hands_python` will run unrestricted by Hands filesystem
confinement, with the local proxy user's filesystem permissions. The cwd and
path-scope grants do NOT protect files in unconfined mode, including when the
agent itself runs remotely.

The risk prompt warns before consent that acceptance restarts any existing
Python kernel and loses its variables and imports. The restart happens only
after operator approval, never while waiting for consent.

This risk approval is separate from wrapper permissions, path grants, and taint
acknowledgement. Acceptance is in-memory, session-only, never persisted, and
never transferred to another session. Without explicit acceptance, no child is
spawned. Rejection is final for the session. Cancellation, timeout, malformed
responses, stale replies, and a missing approval-capable client also cannot
authorize execution. Risk prompts are bounded; the agent must not retry a refusal.

Disconnecting any hosted MCP connection revokes the risk grant for its shared
session and retires that session's Python kernel. Other connections to the same
session do not keep the grant. The session's one-risk-request limit and final
request history survive this connection reset. Reconnecting therefore cannot
request risk approval again, even if the operator previously accepted it.
Execution stays blocked with no active risk grant; this does not mean the
operator rejected the earlier request. Start a new ACP session, or load a session
as a new provider-session incarnation, to request fresh operator approval and
start a fresh Python namespace. With an available confinement backend, execution
can continue confined after connection reset without risk approval.

Scope-query results report unconfined mode in their `message`, and shell/Python
execution results report it in `stderr`. The five-tool wire contract and result
schemas do not change. An approved path list is not a security boundary in this
mode. If a backend later becomes available, that does not retroactively confine
already-running processes; a scope query must not imply otherwise.
Filtered environment, safe stdin, output capture, and other non-OS hardening
remain in effect. Risk acceptance and refusal are audited without command text,
file contents, or credentials.

#### Confined path requests

Use `hands_request_scope(path)` proactively before shell or Python needs a path
outside the approved set. `path=""` queries that set without prompting. The wire
method is `request_scope` with exactly `{path: string}`. Its result is exactly
`{approved: bool, paths: list[string], message: string}`. Non-empty requests go
through the provider to the editor's `session/request_permission` channel. Only
operator approval adds the exact path, never its parent or a glob. Scope prompts
and scope audit events contain the path and fixed status, not commands or file
contents. Scope outcomes use the existing `acp_permission_outcome` event name,
written as bounded JSON records to the local proxy's stderr. These records do
not use ACP stdout and are not persisted in the daemon journal. A rejected path is final for that session: do not request or retry it.

Approval affects the next shell call. It restarts the persistent Python kernel,
losing REPL variables and state; the approval prompt states that cost. Approved
paths are session-local and disappear with the session. This does not widen
`hands_read` or `hands_edit`, clear ingest labels, acknowledge taint, or grant
trusted host execution. Those gates remain separate.

Empty or surprising output may be genuine or caused by confinement. Programs can
swallow permission errors; absence of output is not proof of denial. Diagnostics
are best effort. Query the approved set and request needed paths proactively
rather than waiting for a reliable refused-path report.

`hands_request_scope` is a narrow authenticated ACP admin operation. It is routed
normally; it does not use reusable wrapper execution permissions. It adds a tool
to the existing `mimir.hands.v1` exact profile. Old peers will fail strict profile
admission, so update the admin's proxy and server together. There is no separate
consumer compatibility mode or change to shell/Python arguments.


Process-lifetime limitation: same-process-group children are cleaned up, but
Seatbelt does not provide retroactive revocation of a running process's profile.
A child can detach with `setsid` (the probe succeeds even with
`deny process-info*`) and may outlive the session. Such a detached process keeps
its original OS confinement profile. Ending the session deletes in-memory scope
grants; it does not revoke paths from already-running detached processes.
