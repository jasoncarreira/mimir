# Mimir Hands: proxy-hosted REPL with client-side authorization

**Status:** design note, not a decision. Nothing here is built.
**Written against:** `feature/acp` @ `7deaf4ad`, from live measurement (harness: `~/projects/odin/acp-test-harness`).
**Related:** GitHub #1592, chainlink #1308, the Hands findings A/B/C.

## Proposal in one line

Move the Hands provider from the *editor* into mimir's own `mimir acp` proxy, move Hands
authorization from mimir's IFC sink gate to ACP's `session/request_permission`, and only then
collapse the tool surface to a single persistent Python REPL.

**The three parts are one proposal.** The REPL alone is worse than what exists today; see
"Why one tool only works with part 2".

## Why the current design is stuck

Measured on a live daemon with a fully compliant client-side provider (correct `mcp/connect`,
exact tool schemas, valid responses):

- **A.** `tools/call` accepts only a *bare* structured object. The MCP-spec `CallToolResult`
  (`content` + `structuredContent`) is rejected as `hands_read returned a malformed result`,
  even though `tools/list` advertises `outputSchema`, which in MCP *means* return
  `structuredContent`. A spec-compliant editor fails every call.
- **B.** `hands_edit` and `hands_shell` are refused before reaching the client
  (`ifc_label_blocked:shell_process` / "permission was rejected before execution").
  `session/request_permission` count across every run: **0**.
- **C.** Declaring the provider also breaks plain `shell_exec` in that session — verified with a
  same-session discriminating pair.

Two structural problems sit behind those:

1. It requires **editors** to implement a mimir-specific provider. They won't.
2. It only works on clients that support client-hosted ACP-type MCP servers at all.
   Prime Agent's ACP mode, for example, accepts only stdio and HTTP servers.

## Part 1 — host the provider in the proxy

`mimir/acp/proxy.py` is pure pass-through today (verified: no MCP handling). But it already
rewrites request params: `_write_frame` injects the web key into `authenticate`. The same
mechanism can inject `mcpServers` into `session/new`.

Consequences:

- mimir owns both ends of the provider wire, so finding A stops being an interop problem
- works with **any** stock ACP client, including ones with no client-hosted-tool support
- a Python kernel embeds naturally — the proxy is already an asyncio Python process on the
  client host
- the proxy is already the credential boundary, so it is the honest place for host-side authority
- the editor never needs to know Hands exists

## Part 2 — authorization moves to the client

ACP already puts host-resource authorization in `session/request_permission`. Mimir has the
machinery and it is **dormant** — zero requests observed, including for admin-tier tools.

The client owns the host. Mimir cannot see the client's filesystem and currently reasons about
it badly. With per-call approval that shows the actual code, an opaque tool becomes acceptable:
the human sees exactly what will run.

Mimir's sink gate then classifies the whole Hands surface as one honestly-named
"client-authorized host execution" capability, instead of pretending to adjudicate resources it
cannot observe.

## Why one tool only works with part 2

The sink gate classifies **per tool** into `SinkCategory`. Three narrow tools map cleanly onto
`FILE` and `SHELL_PROCESS`. One opaque "run arbitrary Python" tool can only honestly be
classified as shell-class — and shell-class is precisely the path already failing
(`ifc_label_blocked:shell_process`, measured).

So a REPL-only Hands would make *reading a file* take the most-blocked path in the system.
**Absent part 2, three narrow tools are strictly better than one REPL.**

## What we would not take from prime-agent

`prime-agent-runtime` is a separable MIT Python package, but it is a recursion shim
(`ipykernel`, `mcp`, `nest-asyncio`, `tyro`; ~87 KB across five files) whose purpose is letting
`rlm()` call back into a 12.2 MB TypeScript host. Strip the recursion semantics and what remains
is `ipykernel`. PR #1687 ("Complete the REPL cutover: remove IPython, ZMQ, and cell magics") is
open, so the kernel implementation is in flux besides.

**Take the pattern, not the dependency.**

### And note what the pattern does *not* come with

Prime Agent has **no REPL sandbox and no shipped design for one**. Searched issues, PRs, and
discussions on 2026-08-24:

| item | what it is | state |
|---|---|---|
| [#896](https://github.com/PrimeIntellect-ai/prime-agent/issues/896) | scoped execution + caller-granted capabilities — our exact question | closed `not_planned` in a queue sweep, never addressed |
| [#305](https://github.com/PrimeIntellect-ai/prime-agent/pull/305) | in-kernel enforcement via `sys.addaudithook` + bwrap/sandbox-exec | **open, unmerged** |
| [#1427](https://github.com/PrimeIntellect-ai/prime-agent/pull/1427) | Codex-style OS sandbox at kernel spawn | closed **unmerged** |
| [#1120](https://github.com/PrimeIntellect-ai/prime-agent/issues/1120) / [#1126](https://github.com/PrimeIntellect-ai/prime-agent/pull/1126) | sandboxing *documentation* | closed `not_planned` / unmerged — no security page exists in `docs/` |

#896 frames the dilemma more sharply than this note originally did:

> Today the only built-in tool is IPython, so the practical choices are:
> `No IPython: insufficient for multi-step execution` /
> `IPython: full host filesystem, process, environment, and network access`

The README is explicit: *"Prime Agent executes model-generated Python and project commands with
your user permissions"*, and the daemon/worker/kernel split is **"not a security sandbox."**

**So adopting one-REPL Hands means doing the thing the reference implementation has not managed
to do.** That is a different risk profile from reusing a solved component, and this note should
not be read as implying otherwise.

Corroboration from an integrator in exactly mimir's position — embedding prime-agent behind an
external control plane ([discussion #1402](https://github.com/PrimeIntellect-ai/prime-agent/discussions/1402)):

> a model or resumed session can create model calls and code execution outside the embedding
> control plane lifecycle

That is what happens to mimir's authz layer if it hosts a REPL it cannot bound.

## Authorization is not information flow

The most important thing not to conflate. Client-side permission authorizes the **operation**.
It says nothing about the **result**.

Bytes returned from the client host are, by definition, content the agent did not author — they
must still be labelled untrusted active ingest, exactly like any other external read. That is
mimir's job and it does not move to the client.

Which means **this design does not bypass #1592, and is blocked by it.** Today an untrusted read
produces a deliberately incomplete `SourceLabel` (`access_control.py:8295` → `_incomplete_protected_result`:
`principal=None`, `bridge_instance=None`, empty ACL) that fails `is_complete` on the first line of
`_source_is_triggering_channel_compatible`, so the turn cannot reply at all. A Hands REPL that
reads the user's worktree hits that on every call.

## Open questions

1. **Sandbox posture — a three-way choice, not an inherited one.**

   First, separate two things this note originally ran together: **consent is not containment.**
   Part 2 (`session/request_permission`) buys *consent* — a human approves each call with the code
   visible. It buys no *containment*. Prime Agent's #305 attempts containment and has no consent
   layer. Neither alone is a security boundary.

   The options, with what each actually costs:

   - **(a) Consent only.** Client approves every REPL call, code visible; no runtime restriction.
     Cheapest, and honest about what it is. Load-bearing assumption: a human can meaningfully
     review generated Python *per call*. If approval fatigue drives allow-always, this degrades to
     no boundary at all. This is now the same question as open question 2 — they are one decision.
   - **(b) In-kernel containment** — `sys.addaudithook` + host-held toggle token, per #305.
     Sound in principle: CPython genuinely does not permit removing an audit hook. But it guards
     *Python-level* operations only; a subprocess escapes unless separately mediated, which is why
     #305 also needs bwrap/sandbox-exec. #305's own stated risks apply to us: guard-setup failure
     can abort kernel startup, and *"subprocess sandboxing has platform-dependent fallback
     behavior."* Unmerged upstream for months.
   - **(c) OS sandbox at kernel spawn** — `sandbox-exec` (macOS) / bwrap (Linux), per #1427.
     Strongest containment, but three different security postures depending on the client's OS,
     and Hands runs on *the user's* machine, so we inherit that variance rather than controlling
     it. Rejected upstream (unmerged, drive-by).

   Whichever is chosen, the posture is **not** inherited from `shell_exec`. That risk was accepted
   for a single operator driving their own agent; Hands is a *remote* agent driving the user's
   host. State the choice explicitly and record why.
2. **Permission granularity — decided together with 1(a).** Per-call approval on every REPL call
   may be unusable in practice. ACP permission options support allow-once vs allow-always; which,
   and scoped to what? If the answer is allow-always-for-session, option (a) stops being a boundary
   and (b) or (c) becomes mandatory rather than optional.
3. **What the sink gate still owes.** If the client authorizes operations, what does mimir's gate
   add? Proposed answer: nothing on authorization, everything on labelling results. Worth
   confirming rather than assuming.
4. **Session/cwd binding.** One kernel per ACP session, cwd from `session/new`. Interacts with the
   multi-connection question — per-worktree sessions want per-worktree kernels.
5. **Does part 1 change the trust boundary?** A proxy-hosted provider is mimir's own code running
   on the user's machine, versus editor-hosted code. Arguably better (we control it), arguably
   worse (users audit their editor, not our proxy). State the choice explicitly.

## Sequencing

1. **#1592 first.** Without it, Hands returns bytes that silence the turn. Smallest change,
   load-bearing for everything else.
2. **Finding A** — resolvable independently, and cheap; may become moot under part 1, so decide
   part 1 first or accept the throwaway.
3. **This design**, parts 1 and 2 together.
4. Only then consider collapsing to one tool.

## Acceptance criteria sketch

- [ ] A stock ACP client with **no** client-hosted-tool support can use Hands.
- [ ] Whatever shape `tools/call` accepts is the shape `tools/list` advertises via `outputSchema`.
- [ ] Every Hands operation issues `session/request_permission` before execution, carrying the
      code to be run.
- [ ] A denied permission fails the tool call cleanly and the turn can still report why.
- [ ] Hands results are labelled untrusted active ingest and cannot flow cross-channel.
- [ ] A turn that used Hands can still reply to its originating ACP channel (requires #1592).
- [ ] No change to what a non-ACP turn may execute.
