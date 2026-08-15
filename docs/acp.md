# ACP client

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

ACP is enabled when `MIMIR_ACP_ENABLED` is unset or true and disabled when it is false. The daemon listens at `$MIMIR_HOME/.mimir/acp/daemon.sock`. The `.mimir/acp` directory is owned by the daemon UID with mode `0700`; the socket is owner-only with mode `0600`.

stdin and stdout carry UTF-8 JSONL ACP frames only. stdout is reserved before command imports and diagnostics go to stderr. Local socket and relay connection attempts are bounded to 5 seconds. SSH process creation and establishment are bounded to 12 seconds. Once established, a session has no duration limit. Cleanup uses writer drain, close, and abort bounds of 2, 1, and 1 seconds within a 5-second force-close bound, then waits 1 second for SSH, terminates and waits 2 seconds, and kills and waits 1 second.

## Profiles and credentials

Create and manage profiles with these commands, replacing `PROFILE` and absolute paths:

```sh
mimir acp profile add-local PROFILE --home /absolute/server/mimir-home
mimir acp profile add-ssh PROFILE --home /absolute/server/mimir-home \
  --ssh-host host.example --ssh-user mimir --ssh-port 22 \
  --identity-file /absolute/id_ed25519 --known-hosts-file /absolute/known_hosts
mimir acp profile list
mimir acp profile remove PROFILE
mimir acp credential add PROFILE
mimir acp credential replace PROFILE
mimir acp credential remove PROFILE
mimir acp credential list
mimir acp --profile PROFILE
```

Profiles contain only non-secret target, home, socket, and SSH identity data. They are stored in `${XDG_CONFIG_HOME:-~/.config}/mimir/acp/profiles.json`. `MIMIR_ACP_PROFILE` may select a non-secret profile name only; it must never contain a key.

On the server, issue the existing named admin web credential:

```sh
mimir identities issue-key --home /absolute/server/mimir-home CANONICAL --admin
```

On the client, run `mimir acp credential add PROFILE`. Enrollment reads the value without echo from a controlling TTY, not stdin. The raw key exists only in the client's native OS credential store under service `mimir.acp`; the server stores only its hash. There is no plaintext or third-party fallback, and enrollment fails if no secure backend exists. If the native store raises after a credential mutation was dispatched, the command exits 3 with `credential-mutation-uncertain`; inspect the native store before retrying. Other validation, profile, secure-store selection, read, and TTY failures exit 1. The raw key must never appear as an SSH password, in `sshpass` or PAM reuse, argv, an environment variable, editor JSON, profile JSON, or registry data. `MIMIR_API_KEY` supplies transport/route authority and is not the ACP principal key.

A stock client sends `authenticate` with only `methodId`. The proxy injects proof only into the protected upstream authenticate request. The daemon resolves it to a non-service admin identity and constructs `AuthContext` server-side. Per-call ACP permission is a second factor and cannot create authority.

Validate enrollment by launching `mimir acp --profile PROFILE` from a stock client and completing its ordinary `authenticate` exchange. There is no `credential validate` command or pre-activation validation protocol.

Rotate in this exact order:

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

There is one active ACP connection per `MIMIR_HOME`. Only a newly authenticated connection can supersede the prior generation; failed or partial authentication cannot evict the active client. Reconnection creates a new authentication and generation boundary. Session IDs are owner-bound UUIDv4 values, and reconnection resumes them through `session/load`; provider, permission, and MCP request identities are fresh.

The journal has a default seven-day TTL and a 64 MiB limit. Before replay, Mimir revalidates the provider. A load replays every durably prepared `session/update` with its original sequence, including records already sent. Clients must tolerate duplicates. Replay never re-executes effects. Pending requests and frames are not replayed, external effects are not exactly-once, and cancellation does not roll back completed effects.

Transport death cancels and quarantines only that ACP generation. The daemon, web UI, bridges, scheduler, unrelated work, and completed effects remain alive.

## Providers, permissions, and filesystems

Sessions are providerless by default. The sole optional client-hosted provider is one MCP-over-ACP declaration named `mimir-hands` with profile `mimir.hands.v1` and exactly the `read`, `edit`, and `shell` tools. It is validated afresh on session new, session load, and provider-list change. Permission decisions are one-shot `allow_once` or `reject_once`. Arbitrary or multiple providers are rejected; an IDE's integrated MCP server is not Mimir Hands.

Native Mimir tools operate on the daemon host. Mimir Hands operates on the client host and returns opaque `client-file:` resources. Its `cwd` is context, not filesystem confinement or a path sandbox. Mimir tolerates advertised ACP client `fs` and `terminal` capabilities but never calls them. `additionalDirectories` and arbitrary provider profiles are rejected.

## Troubleshooting

The proxy intentionally reports the generic diagnostic `error: connection-failed`. Confirm the selected profile, then confirm `mimir run` is running with `MIMIR_ACP_ENABLED` unset or true. As the owner UID, inspect `<MIMIR_HOME>/.mimir/acp`: the directory must be mode `0700`, and `daemon.sock` must be mode `0600`. Start or restart `mimir run` if the daemon is missing or disabled; the proxy will not start it.

For SSH profiles, additionally confirm the remote `mimir-agent` version is 0.9.0, it is on the noninteractive PATH, identity and known-hosts permissions are correct, the host-key entry matches, and remote stdout is banner-free.
