# Testing ACP

How the ACP surface is tested, what each layer covers, and what it does not.
Companion to [`docs/acp.md`](../acp.md), which describes the architecture itself.

Testing splits into two layers that catch different things:

| layer | what it is | catches | cost |
|---|---|---|---|
| **Automated suite** | 472 tests across 21 files, in the normal `pytest` run | protocol shape, state machines, refusal paths, packaging | seconds, every CI run |
| **Live harness** | 4 scripts driving a real daemon socket | interop, delivery, trust boundaries, provider paths | manual, needs a running agent |

The split matters because **every ACP defect found by measurement so far was invisible
to the automated suite** — not because the suite is weak, but because the failures were
in what a real client observes over a real socket, which unit tests mock away.

---

## Layer 1 — the automated suite

Runs in the normal `uv run pytest -q`. No daemon, no socket, no network.

| file | tests | covers |
|---|---:|---|
| `test_acp_sdk_contract.py` | 83 | the contract against the vendored SDK shape |
| `test_acp_sessions.py` | 61 | session lifecycle, replay, cancellation |
| `test_acp_daemon.py` | 47 | socket ownership/mode, accept loop, shutdown |
| `test_acp_profiles.py` | 33 | profile add/list/remove, local and SSH |
| `test_acp_bootstrap.py` | 30 | enablement gating on `MIMIR_ACP_ENABLED` |
| `test_acp_agent.py` | 27 | agent-side method dispatch |
| `test_acp_journal.py` | 25 | journal records and redaction |
| `test_acp_updates.py` | 24 | `sessionUpdate` emission |
| `test_client_provider.py` | 21 | the client-provider `tools/call` contract for all three Hands tools |
| `test_acp_registry.py` | 17 | registry manifest and eligibility |
| `test_acp_transport.py` | 15 | framing and transport errors |
| `test_acp_credentials.py` | 15 | credential add/replace/remove |
| `test_acp_shutdown.py` | 13 | drain and teardown ordering |
| `test_acp_dependency_closure.py` | 13 | the import closure stays inside the declared extra |
| `test_acp_ssh.py` | 12 | SSH transport construction |
| `test_acp_proxy.py` | 12 | the stdio proxy, including `_write_frame` key injection |
| `test_acp_packaging.py` | 7 | packaging metadata |
| `test_acp_bridge.py` | 6 | bridge wiring |
| `test_acp_relay.py` | 5 | the credential-blind relay |
| `test_assert_installed_acp.py` | 4 | the CI installed-artifact probe itself |
| `test_acp_stdio.py` | 2 | stdio plumbing |

Counts measured with `--collect-only` at the head of this branch.

### What CI additionally enforces

Two helpers run in the **`package`** job, against built artifacts rather than the source tree:

```
python .github/assert_installed_acp.py dist/direct/*.whl
python .github/assert_installed_acp.py dist/*.tar.gz
```

`assert_installed_acp.py` installs the built wheel and sdist into a clean environment and
probes that the ACP entry points resolve there. `acp_dependency_closure.py` walks the
import graph with `ast` and asserts the ACP modules pull nothing outside their declared
extra — so an accidental import cannot make ACP a hard dependency of a base install.

This is the layer that catches "works in the repo, broken in the wheel".

---

## Layer 2 — the live harness

Location: `~/projects/odin/acp-test-harness` (a scratch directory, **not** in the repo).

```
acp_client.py        minimal stock-ACP client over the daemon socket
acp_scenario5.py     tool vs no-tool message delivery
acp_scenario7.py     per-path trust boundaries
acp_hands.py         client-hosted MCP ("hands") provider, end to end
hands-tools.json     tool manifest the fake provider advertises
```

### Prerequisites

1. A running `mimir run` with `MIMIR_ACP_ENABLED=true`. The proxy never starts the agent;
   if the daemon is absent you get the deliberately generic `error: connection-failed`.
2. The daemon socket at `$MIMIR_HOME/.mimir/acp/daemon.sock` — the harness hardcodes
   `/tmp/acp-h/.mimir/acp/daemon.sock`, so either run the agent with `MIMIR_HOME=/tmp/acp-h`
   or edit `SOCK` in `acp_client.py` and `acp_hands.py`.
3. A web key, issued once and shown once. The scripts parse it out of the issuance output
   with `re.search(r"│\s+(\S{24,})", ...)` — i.e. they read the box-drawn "COPY NOW" panel
   directly, so save that output to a file and pass the path as `argv[1]`.

```
python acp_scenario5.py acp-key.out
python acp_scenario7.py acp-key.out
python acp_hands.py     acp-key.out hands-tools.json
```

### `acp_client.py` — the client under everything

Not a test itself; the ~90-line client the scenarios import. Worth knowing because its
behaviour defines what the scenarios can observe:

- speaks newline-delimited JSON-RPC straight to the Unix socket
- **auto-answers `session/request_permission`** by picking the first option whose
  `optionId` contains `allow`. So scenarios exercise the *approve* path by default;
  to test denial you must change this.
- logs three message classes separately — responses, **server→client requests**, and
  notifications — which is what makes "the agent never asked" visible rather than silent
- distinguishes `id: null` unsolicited frames, which is how protocol violations surface

### `acp_scenario5.py` — does agent text actually reach the client?

Three prompts in one session: no-tool, tool, no-tool again. Counts
`agent_message_chunk` updates for each.

The point is the **comparison**, not any single number. Equal counts mean tool use doesn't
suppress delivery; a zero in the middle means it does. Running the no-tool prompt twice
brackets the tool prompt so a general session-wide degradation is distinguishable from a
tool-specific one.

### `acp_scenario7.py` — trust boundaries by path

Reads three paths in one session and reports what was delivered:

| path | expectation |
|---|---|
| `docs/trust-probe.md` | newly trusted by #1591 |
| `memory/core/00-identity.md` | already trusted |
| `saga.toml` (home root) | untrusted |

A read that succeeds but delivers nothing is the interesting outcome — that is the
information-flow gate blocking the *reply* rather than the *read*, which looks identical
to a broken tool from the client side.

### `acp_hands.py` — the client-hosted provider path

The most involved script. It impersonates an editor that hosts an MCP server, so it must
serve, not just call:

- `mcp/connect` → returns a `connectionId`
- `mcp/message` → dispatches `initialize`, `tools/list`, `tools/call`
- `session/request_permission` → auto-allows

`HANDS_BARE=1` switches `tools/call` between two response shapes: a **bare structured
object** and the MCP-spec `CallToolResult` (`content` + `structuredContent`). That toggle
exists specifically to demonstrate finding **A** below — it is the whole experiment.

`HANDS_PROMPT` overrides the prompt text.

---

## What live testing established

Measured against a fully compliant client-side provider — correct `mcp/connect`, exact tool
schemas, valid responses. These are the findings that justified the Hands redesign
(`ACP_HANDS_REPL_DESIGN.md`, alongside this file on this branch):

**A. `tools/call` accepted only a bare structured object — FIXED, and now inverted.**
When measured, the MCP-spec `CallToolResult` shape was rejected as `hands_read returned a
malformed result`, even though `tools/list` advertises `outputSchema`, which in MCP *means*
return `structuredContent`. A spec-compliant editor failed every call.

That is no longer current behaviour and this section previously read as though it were.
On this branch `mimir/acp/agent.py` **requires** `structuredContent` and raises
`Client provider result is missing structuredContent` without it — so the spec shape is
what is accepted and the bare object is what is rejected, the reverse of the finding.
`tests/test_client_provider.py` pins that contract for all three Hands tools. `HANDS_BARE`
toggles between the two shapes so the harness can still exercise the rejected one.

**B. `hands_edit` and `hands_shell` are refused before reaching the client**
(`ifc_label_blocked:shell_process`). `session/request_permission` count across every run:
**0**. The permission machinery exists and is dormant.

**C. Declaring the provider breaks plain `shell_exec` in the same session** — verified with
a same-session discriminating pair.

**B and C** are not visible to the automated suite — each required a real client, a real
socket and a real prompt. **A is now covered** by `tests/test_client_provider.py`, which is
why it is listed in Layer 1 above; the live harness is what found it, and the automated
suite is what keeps it fixed.

---

## Gaps

Known and worth stating rather than discovering again:

- **Denial paths are unexercised.** Both clients auto-approve permission requests. Nothing
  currently drives a `cancelled` outcome or a rejected option.
- **The harness lives outside the repo**, so it is not versioned with the code it tests and
  will drift. It also hardcodes `/tmp/acp-h`.
- **The key parser is fragile** — it regexes a box-drawn CLI panel. A cosmetic change to
  that output breaks every scenario with a confusing failure.
- **No automated interop test.** Findings A/B/C were all found by hand; nothing would catch
  a regression in any of them.
- **Registry eligibility is blocked** for a separate reason: the candidate advertises no
  `authMethods`, so `registry/mimir/agent.json` is a schema-valid rehearsal, not a
  submittable entry. See `docs/acp.md`.
- **Real editors are only smoke-tested** (JetBrains AI Assistant, Zed, VS Code — see
  `docs/acp.md`). Prime Agent's ACP mode accepts only stdio and HTTP servers, so it cannot
  exercise the client-hosted provider path at all.
