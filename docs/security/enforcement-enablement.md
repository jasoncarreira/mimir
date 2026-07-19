# Enforcement enablement: sink classification and poller capabilities

**Status:** DRAFT — revision 4. Incorporates mimir review rounds 1–3 (spawn and
`worklink_run` are not process-confined; the "scoped" file root is actually the
whole home; the `SAGA` category is too broad; integrity is an enablement
prerequisite; approved-URL authorizes the request, not the response bytes) and
the operator's decisions (per-trigger policy; approved-URL = egress-only;
trust the contributor / JIRA instance wholesale; provenance schema; auto-recall
must never handcuff a user turn; future isolation must run in docker **and** AWS
ECS/Fargate → no bubblewrap). The open design questions from rev 3 are now
resolved (§6).
**Date:** 2026-07-19.
**Context:** written after a whole-`access_control` adversarial review (5 parallel
reviewers, findings verified with runtime repros). It proposes the model that
lets `MIMIR_ACCESS_CONTROL_ENFORCED` be turned on **without making the agent
useless**. It complements the authoritative reference in
[`../authorization.md`](../authorization.md) and the earlier design artifact in
[`requester-resource-authorization.md`](requester-resource-authorization.md).

---

## 1. Why this doc exists

The adversarial review's conclusion: the authorization **security architecture
is sound** — no reviewer could construct a cross-user leak; SAGA read-scoping,
IFC label propagation, declassification, the `#906` poller hard-block, catalog
completeness, and the enable-time gate all hold up. SAGA scoping is even
already-on regardless of the flag.

But **enabling enforcement today would cripple the agent**, and that's the
blocker. Two over-blocks (both verified by runtime repro):

1. **Human/operator turns can't act.** Every turn is self-tainted at ingress
   (a `{private}` channel source). In `_get_allowed_sinks` (`mimir/access_control.py`),
   after the service branches, there is literally:

   ```python
   if category != SinkCategory.SAME_CHANNEL:
       return frozenset()
   ```

   So for any non-service (human) principal, **every** action/egress sink —
   `shell`, `spawn_*`, `write_file`, `memory_store`, `add_schedule`,
   `open_proposal`, `fetch_url` — is denied on a normal turn. The operator can
   read and reply, and nothing else. (PR #1138 already carved out the
   same-channel *reply* after a protected read; it did **not** carve out
   actions.)

2. **Pollers can't do their real work.** There is one generic `poller` service
   principal (trigger `poller`). It is *granted* real capabilities (`spawn_*`,
   `write_file`, `worklink_run`) **and** has containment policies for them
   (`spawn_workspace`, `configured_file_roots`, `shell_profile=scheduler_read_only`,
   `worklink_repo`). But because `poller_payload` is in its `readable_domains`,
   the `#906` block fires first and blanket-denies every action/egress category:

   ```python
   if "poller_payload" in service.readable_domains and category in {
       SinkCategory.SHELL_PROCESS, SinkCategory.SPAWN, SinkCategory.FILE,
       SinkCategory.NOTIFICATION, SinkCategory.HTTP_WEBHOOK,
       SinkCategory.NETWORK, SinkCategory.EXTERNAL_MCP,
   }:
       return frozenset()
   ```

   So the capabilities and containment policies are dead code. A poller can only
   `read` + `send_message`. Concretely:
   - the **GitHub poller** can't do code work (develop, or update/test from a
     review) — no `spawn`/`worklink`;
   - **research pollers** (including a plain RSS reader) can't save what they
     find — file-write is `#906`-killed, and memory-write (`memory_store`/`saga_*`)
     **isn't even in the poller principal's capability list** at all.

   Enforcement-as-built makes pollers pointless.

**The point of enforcement is to stop leaks and injection-driven actions — not
to stop the agent from doing its job.** This doc proposes how to do the former
without the latter.

---

## 2. The reframe: classify sinks by **blast radius**, not by data-trust

The current model asks *"is this data untrusted?"* → if yes, block everything.
That's why it's all-or-nothing. The better question is:

> **How big, reversible, and reviewable is this sink's effect?**

The `#906` intuition ("don't let attacker-controlled payload drive a sink") is
correct for **unbounded** sinks and wrong for **contained** ones. The examples
you already rely on are contained-by-construction:

- **Contained by scope** (safe regardless of content trust): a scoped
  `write_file` to a **narrow per-trigger state root** (core memory and system
  paths un-writable; see §5.1 on why the root must be narrow), a read-only
  `shell`, or a provenance-tagged **memory write** to the recallable store
  (§5.3). These are bounded because the *destination* is bounded — the content
  driving them can't reach beyond it.
- **Code work** = `worklink_run`. It has an isolated Git worktree
  (`create_isolated_checkout`), `observe_evidence` diff/test validation, and a
  reviewed-PR-only durable output — so its **git/review blast radius** is
  bounded. This is safe for **trusted** code work (a known contributor's PR,
  your own request, a heartbeat).

> **Important correction (mimir review, rounds 1–2):** "code work" is **not
> process-confined**, so it is safe only for **trusted** content — not for
> untrusted payloads.
> - Generic `spawn_*` (`spawn_open_code` / `spawn_codex` / `spawn_claude_code`)
>   isn't even git/review-contained: it runs the CLI as an ordinary subprocess
>   whose only confinement is `spawn_workspace`
>   (`_target_within_configured_write_roots` = **all of `MIMIR_HOME` + configured
>   RW roots**), with no read-only guard and no PR postcondition.
> - `worklink_run`'s worktree is only a **`cwd`**, not a sandbox. Its compute
>   (`LocalSubprocessComputeBackend`) reports `shared_filesystem=True,
>   network_isolated=False` and launches the CLI with `HOME` + provider creds —
>   so the child can **write outside the worktree and reach the network freely**;
>   the diff-review only inspects the *worktree's* git diff, not process side
>   effects. (The registry already flags this "unsafe by caps.")
>
> So **neither `worklink_run` nor `spawn_*` may run untrusted code.** Untrusted
> code work is **notify-only** (§4). Autonomous untrusted code work would require
> a genuinely isolated compute substrate (`ComputeCaps` with
> `not shared_filesystem and network_isolated`) — **which nothing currently
> provides** (docker was removed). That's a prerequisite, not present today
> (§5.5, §6).

So the rule is two-dimensional (§4): **trust of the content × blast-radius of the
sink.** Untrusted content may drive only scope-contained sinks (narrow file/state
writes, read-only shell, provenance-tagged memory) — never code execution or
network egress. Trusted content may drive code work (`worklink_run`) and, per
capability, the rest.

---

## 3. Sink tiers

| Tier | Sinks | Why | Untrusted content | Trusted content |
|---|---|---|---|---|
| **Scope-contained** | `write_file`/`edit_file` to **narrow per-trigger roots**, read-only `shell` | the destination is bounded, so the content can't reach past it | **Allow** (per capability) | Allow |
| **Scoped-with-provenance** | `memory_store` / `saga_*` **create-atom + feedback/credit** to the recallable store | usable later, tagged with origin; can't reach core memory | **Allow**, tagged untrusted | Allow |
| **Code execution** | `worklink_run` (git/review-contained, **not** process-confined); generic `spawn_*` (not even git-contained) | runs a coding CLI with full filesystem + network + creds; only trusted code is safe to run this way today | **Block → notify-only** (needs an isolated compute substrate to ever run untrusted code — §5.5) | Allow (`worklink_run`; `spawn_*` only with the §5.5 isolation contract) |
| **Unbounded / exfiltrating** | `fetch_url` / `NETWORK`, webhooks, `EXTERNAL_MCP`; write-`shell` on the live host; writes to core memory / prompts / system paths | leaves the trust boundary or is irreversible/self-modifying | **Hard-block** (`#906`) | Allow only via the egress boundary (heartbeat approved-URL list; user ask-on-first-use) — §5.4 |

---

## 4. Trust model and per-trigger policy

Two **independent** inputs decide what a turn may do:

1. **Capability set** — declared per trigger type (a poller's manifest, or the
   trigger's built-in profile) and validated against the tier table (§3). A
   trigger can only ever use sinks in its declared set. This is the *ceiling*.
2. **Content trust (integrity)** — derived from a source the content **cannot
   forge**, *not* from the fact that a trusted party started the turn:
   - **Internal trigger** (heartbeat, session-boundary, operator's own typed
     input) → **trusted**.
   - **GitHub content** → trusted iff the author is a **repo collaborator, org
     member, or has write access** (GitHub's own permission graph — operator-
     controlled, un-forgeable by a PR). Such a contributor's issue/PR is trusted
     **as a whole**, including material it embeds/quotes or is built on top of —
     we trust the contributor not to introduce malicious content (operator
     decision). The only untrusted github content is from **non-contributors**
     (unknown authors, comments by non-contributors) → untrusted.
   - **A trusted external system we point at** (e.g. a JIRA instance) → trusted,
     on the basis that its admins gate who can file/assign and won't route
     untrusted issues to us (operator decision). Declared per trigger like any
     other trust source.
   - **`fetch_url` from an operator-approved URL** (heartbeat) → the allowlist is
     **egress authorization only** (which hosts may be fetched); the fetched
     **content stays untrusted** (§5.4). Approving a host is not vouching for its
     bytes.
   - Everything else ingested from outside → **untrusted**.

The gate is the **2×2 of content-trust × sink blast-radius** (§3): *trusted →
any sink in the capability set; untrusted → Contained or Scoped-with-provenance
only; untrusted → Unbounded is blocked* (or explicit one-use declassification).
This is the integrity model **anchored on identity**. It replaces "trust the
turn because a trusted party started it," which does **not** survive the
confused-deputy case — untrusted content (an issue body, a web page, a comment)
folded into a trusted turn and then driving an action.

### Per-trigger policy

| Trigger | Capability set (the ceiling) | Trust / gating |
|---|---|---|
| **Operator / user turn** | full (subject to admin tier) | operator's typed input is trusted; untrusted content read mid-turn is tainted → can't drive Unbounded sinks without one-use approval |
| **GitHub poller** | `worklink_run` (worktree + reviewed PR), scoped file/edit, read-only shell, `send_message` | **known contributor** (collaborator / org / write) → trusted → full code-work; **unknown author, or any comment by a non-contributor** → untrusted → **notify the operator only**, no autonomous action (operator then directs the agent) |
| **Research / RSS poller** | write memory (create atom + feedback/credit), scoped state file, scoped wiki, `send_message` — **no `fetch_url`, no `spawn`** | ingested web content is untrusted, but the capability set contains **no Unbounded sink**, so it is safe regardless — no per-author gating needed |
| **Heartbeat** | near-full incl. `fetch_url` from an **approved-URL list** | internally triggered → trusted. `fetch_url` is gated by the **destination allowlist**, so it may fetch **any approved URL, repeatedly and in any order** — a prior fetch's untrusted content does not lock it out of further approved fetches. Fetched **content stays untrusted**: it can drive scoped sinks (save state / wiki / memory) but not code/shell, and egress to a **non-approved** destination stays blocked. Allowlist should be **exact URLs / fixed templates, not host wildcards** (§5.4) |
| **Session-boundary turn** | session-boundary writes | internal → trusted |
| **(future) JIRA poller** | write chainlinks, update docs (scoped), write memory | **trusted** — we trust the pointed-at JIRA instance's admins to gate content (operator decision); declared like any other trigger |

The config model must be **open to new trigger types** declaring their own
capability profile + trust source — not hardcoded to the rows above.

---

## 5. Design

### 5.1 Per-trigger capabilities in config (named, tier-validated, narrow roots)

Replace the one-size-fits-all `poller` principal with **per-trigger capability
declarations** (a poller's `pollers.json` manifest; a built-in profile for
heartbeat / session-boundary). Decisions:

- **Named capabilities, not roles** (mimir rec): the manifest lists exact
  capability names, validated against the tier table (§3) at discovery time, so
  it cannot self-grant an Unbounded-tier sink.
- The manifest **cannot mutate or self-grant its own authority declaration** —
  the capability set and its roots come from immutable operator configuration,
  not from anything the poller (or its untrusted payload) can write.
- This also fixes the "research pollers can't write memory" gap: memory-write
  becomes a declarable capability.

**Scoped roots must be narrow and argument-level, not the global file-tool
roots** (mimir blocking finding). The existing `spawn_workspace` /
`configured_file_roots` policies (`_target_within_configured_write_roots`) accept
**all of `MIMIR_HOME` plus every configured `:rw` root** (e.g. `/workspace/mimir`).
That is an operator-wide *reachability* check, not a per-trigger *scoped-state*
capability — under it a research poller could overwrite **another poller's
`skills/<name>/pollers.json` or scripts** (persistence across ticks), edit the
**live source checkout**, or modify **non-core injected memory**. So a poller's
file/state capability must resolve to a **specific, per-trigger root derived from
operator config** — e.g. `state/pollers/<name>/…` and/or explicitly named
knowledge roots — **not** reuse of the global writable roots, and it must not be
able to write another trigger's authority/config.

### 5.2 `#906` becomes tier-based (defer to containment)

In `_get_allowed_sinks`, the `poller_payload` branch stops being a blanket
`return frozenset()`. Instead: for a `poller_payload` turn, a sink is allowed
iff (a) it is in the poller's declared capabilities, (b) it is a Contained or
Scoped-with-provenance tier sink, and (c) the requested destination satisfies
the sink's containment policy (verified, not asserted). Unbounded-tier sinks
stay hard-blocked. This keeps the `#906` guarantee for the sinks that matter
while unblocking contained work.

### 5.3 Scoped memory writes, provenance-tagged (and why **not** a quarantine namespace)

**Scope the operations, not the whole `SAGA` category** (mimir finding). The
`SAGA` sink category today lumps in destructive/governance operations —
`saga_forget`, session-boundary writes (`saga_end_session`), commitment state
changes — with plain atom creation. The research-poller memory capability is
**create/append a provenance-tagged recallable atom, plus feedback/credit**
(`saga_feedback` / `saga_mark_contributions`) — **not** `saga_forget`, **not**
session boundaries (those are created by session-boundary turns). The exact
write ops that carry immutable origin/integrity metadata, and the storage schema
for that metadata, are defined as part of this work rather than inherited from
the coarse category.

Untrusted-derived writes (poller findings) are **usable** memory, tagged with
their origin. We deliberately do **not** route them to a separate "quarantine"
namespace, because a quarantine only has value if something downstream reads it
differently — and recalled memory just flows into the prompt as context. A
quarantine without a down-weighting consumer is either recalled (as dangerous as
un-quarantined) or never recalled (wasted).

**Provenance schema (immutable, server-set on each recallable write).** Rides on
the existing SAGA ownership columns; add:
- `integrity`: `trusted` | `untrusted` (from the trust model, §4, at write time)
- `origin_trigger`: e.g. `research-poller:hn-ai`, `github-poller`, `operator`
- `origin_ref`: the concrete source — URL / issue# / msg-id
- (+ existing `owner_principal`, `origin_channel`, `captured_at`)

None of these are editable by the content or the model.

**Recall is informational, not enforcing** (mimir's "provenance informs, the gate
enforces", applied to recall — and required so an incidental auto-recall never
handcuffs a user turn):
- **Auto-recall** (relevance-based injection at prompt assembly) renders the
  provenance tag so the agent (and operator) can weigh an untrusted-origin fact,
  but it does **not** taint the turn or gate actions. A user turn stays fully
  able to work even if an untrusted memory is recalled into context.
- **Enforcement taint comes only from what the turn actively ingests** — the
  trigger's own content (poller payload, unknown-author issue) and live tool
  reads/fetches this turn — never from ambient recalled memory.

The memory-poisoning defense is therefore: (1) **core memory is always blocked
and PR-gated — pre-existing and universal, for every principal, not
poller-specific** — so untrusted content never becomes an always-loaded trusted
instruction; (2) **provenance visibility** on recall so the agent down-weights
untrusted-origin facts; (3) **the action gate** on anything the turn actively
ingests. Accepted residual: an auto-recalled poisoned fact can *mislead the
agent's reasoning* (it just can't gate-bypass); tainting recall would close that
but break user turns, which is ruled out — on user turns the operator is the
backstop, and on autonomous turns the tight per-trigger capability set bounds
the blast radius.

### 5.4 Network egress: `fetch_url` and the uniform egress boundary

`fetch_url` / `web_search` / webhooks / `EXTERNAL_MCP` are where "let the agent
act" and "let data leak out" are the same action.

**Network egress is gated by the destination allowlist, independent of the turn's
taint.** A request to an approved destination is allowed regardless of what the
turn has ingested; a request to a non-approved destination is blocked. That means
the taint gates *code/shell/action* sinks, not egress-to-an-already-approved
destination — so a heartbeat can fetch **all** its approved URLs, repeatedly and
in any order, without a prior fetch's untrusted content locking it out (this is
the intended behavior, not a hole). By trigger:

- **GitHub / research pollers:** no `fetch_url` capability at all (they fetch via
  their own subprocess; the capability is simply not in their set).
- **Heartbeat:** `fetch_url` allowed against an **operator-approved allowlist** —
  authorization to reach those destinations, **not** a trust signal for the
  response (mimir: approving a host authorizes the request, not the bytes).
  **Fetched content stays untrusted** — it can drive scoped sinks (save state /
  wiki / memory, provenance-tagged) but not code/shell. The heartbeat fetches its
  approved URLs freely.
  - **The allowlist must be exact URLs / fixed templates, not host wildcards.**
    Otherwise untrusted fetched content could steer the agent to a *new*
    data-carrying URL on an approved host (`https://approved/?leak=<secret>`) and
    exfil via that host's logs/reflection — "approved to fetch from" ≠ "safe to
    send arbitrary data to." Exact URLs make taint-independent egress
    unconditionally safe; a genuinely-needed wildcard host would additionally
    require the request to carry no turn-derived data.
- **User / operator turns:** **ask-on-first-use per host** (mimir rec) — the agent
  asks the first time it wants a destination, the operator approves it (adding it
  to the session allowlist), then it's remembered for that scope. Not a blanket
  standing grant, and not an ask on every call.

**One uniform egress boundary, including child processes** (mimir finding).
`fetch_url` is not the only way data leaves the box — **spawned agents and
poller subprocesses have their own network access**. Gating only the agent's
`fetch_url` tool is incomplete. The design must define a single egress boundary
(the approved-host allowlist + one-use declassification semantics) that applies
to child processes too, not a special case for one tool. (This ties to the
`spawn_*` isolation contract in §5.5, which must include egress confinement.)

### 5.5 GitHub poller code work → Worklink only; the `spawn_*` isolation contract

All GitHub-poller code work — greenfield **and** "update/test from an existing
review" — routes through **`worklink_run`** (isolated worktree, `observe_evidence`
diff/test validation, reviewed-PR-only durable output). Generic `spawn_*` is
**not** used for poller code work, because it is not contained (§2/§3).

A known contributor's PR is trusted, so its code work runs; an unknown author's
issue/PR, or a non-contributor comment, is untrusted → **notify-only** (§4). So
even the trusted path is contained by Worklink, and the untrusted path does not
autonomously touch code at all.

**Defense-in-depth now (cheap, worth setting regardless):** run worklink's
opencode with `permission.external_directory: {"/**": "deny"}`, which confines
opencode's **file tools** (read/write/edit/ls/glob/grep) to the working
directory. Note the limits: opencode's permission model is **approval-based**
(`bash`/`edit`/`webfetch` → allow/ask/deny), **not** an OS sandbox — the **shell
is the escape hatch** `external_directory` doesn't close (an allowed `bash`
command can still write outside cwd or reach the network), and a `shell.sandbox:
"strict"` key is **not confirmed** in the opencode docs we can see. So opencode's
config hardens the file-tool surface but is not, by itself, a real sandbox; treat
it as one layer, verify `shell.sandbox` against the opencode version we actually
run, and restrict the `bash` permission.

**Decision: untrusted code work is notify-only for now.** We do **not** build an
isolated compute substrate as part of this enablement. Unknown-author GitHub
issues/PRs (and non-contributor comments) are surfaced to the operator.

**If we later want autonomous untrusted code work**, the requirement is a
`ComputeBackend` whose `capabilities()` reports `shared_filesystem=False,
network_isolated=True` (the registry's existing `unsafe_by_caps` gate then admits
it). **Constraint: it must run in both a docker container and AWS ECS/Fargate —
so no bubblewrap / user-namespace sandboxes** (Fargate grants neither). The
Fargate-compatible substrate (§6) layers: opencode file-permission confinement +
**task-level network egress control** (Fargate security groups / a no-egress or
proxy-only network; `--network` limits under docker) which confines the *whole
task* including any shell `curl` with no per-process netns + optional
**unprivileged, no-namespace** seccomp / Landlock where the kernel supports it.
A new backend behind the existing `ComputeBackend` abstraction; **out of scope**
for the current enablement.

---

## 6. Decisions and remaining questions

**Settled in review (operator + mimir):**

- **Capability schema → named capabilities**, validated against the tier table;
  a manifest cannot self-grant or mutate its own authority (§5.1).
- **Content trust → derived from source identity, not turn ownership** (§4).
  This is the integrity model and it supersedes the earlier "operator-trust"
  framing. Anchors: the GitHub permission graph (collaborator / org / write),
  the operator-approved URL list, and internal triggers. mimir's position — that
  integrity is an *enablement prerequisite*, not a multi-user-someday concern —
  is adopted: the confused-deputy case is closed **now**, single-operator
  included.
- **Network egress → §5.4**: gated by the **destination allowlist, independent of
  turn taint** — so a heartbeat fetches all its approved URLs freely (a prior
  fetch's untrusted content doesn't lock it out). Allowlist = **exact URLs /
  templates, not host wildcards** (closes exfil-via-approved-host). Pollers have
  no `fetch_url`; user turns ask-on-first-use per host; one **uniform egress
  boundary including child processes**, not a per-tool special case.
- **Memory tier → scoped ops, not the whole category** (§5.3): create-atom +
  feedback/credit, provenance-tagged; no `saga_forget` / session-boundary.
- **Provenance schema + recall → §5.3**: `integrity`/`origin_trigger`/`origin_ref`
  stamped immutably server-side. **Auto-recall is informational only — it renders
  provenance but never taints/gates**, so an incidental recall can't handcuff a
  user turn; enforcement taint comes only from what the turn actively ingests.
- **Trust wholesale for trusted sources → §4**: a known GitHub contributor's PR
  is trusted as a whole (embedded/quoted material included); a pointed-at JIRA
  instance is trusted (we trust its admins). Only non-contributor content is
  untrusted.
- **Core memory → always blocked, PR-gated, universal** (pre-existing) — untrusted
  content can never reach the always-loaded set.
- **Untrusted code work → notify-only** (operator decision). We do not build an
  isolated compute substrate for this enablement; unknown-author code work is
  surfaced to the operator (§5.5).

**How would we isolate code execution later** — recorded because the question was
raised; **out of scope** for this enablement, and only relevant if we ever move
untrusted code work off notify-only. Hard constraint: the substrate must run in a
**docker container AND AWS ECS/Fargate**, which grant no user namespaces / no
privileged caps — so **bubblewrap and namespace-based sandboxes are out**. A
future isolated `ComputeBackend` (`shared_filesystem=False, network_isolated=True`;
the registry's `unsafe_by_caps` gate then admits it) would instead layer, in
descending portability:
- **Application-level file confinement:** opencode `external_directory` deny +
  restricted `bash` permission (works anywhere; no kernel deps).
- **Task-level network egress control:** Fargate security groups / a no-egress or
  proxy-only task network (docker: `--network` limits). Confines the *whole task*
  — including any shell `curl` — without a per-process netns, which is the
  Fargate-native way to close the network dimension.
- **Optional, where the kernel allows (unprivileged, no namespaces):** a
  process-installed **seccomp** filter (block socket syscalls) and/or **Landlock**
  (self-restrict writes to the worktree). Kernel-support-dependent under Fargate,
  so defense-in-depth, not the primary control.
- **Credential/env minimization:** no `HOME`/provider creds to the child.

**Remaining open:** none — the rev-3 questions are resolved. Trust is wholesale
for trusted sources (§4); the provenance schema + informational recall are set
(§5.3); JIRA trust is by instance (§4). What's left before flipping the flag is
the implementation work (§8) plus the §7 blockers, not open design questions.

---

## 7. Other enablement blockers (from the review — on the path)

The standalone review findings are **fixed and merged** (2026-07-19, each
masked-check-verified):

- ✅ **`attempted_service` fail-open** → #1140 (sink gate now runs for OPEN tools
  regardless of a spoofed service trigger).
- ✅ **`shell_job_complete` continuation lockout** → #1141 (continuation inherits
  the origin turn's frozen auth, same-channel-guarded, not client-settable).
- ✅ **R1 `protected_prompt` channel-binding** → #1142 (bound to the triggering
  channel; producers stamp the content's origin channel).
- ✅ **R2 `InformationFlowState.merge` monotonicity** → #1143 (regression locking
  taint accumulation).
- ✅ **Enablement hardening batch** → #1144 (inventory assertion covers deepagents
  built-ins + registered MCP; `_env_access_control_enforced` no longer raises in
  `wrap_tool_call`).

**Still open before the flag can flip** (chainlinks #922, #923):
- **#922** — migrate trusted-service autonomous maintenance off raw write-shell
  (the scheduler/poller shell profile is read-only; a write-shell maintenance turn
  breaks on enable). Overlaps with §5 — those turns should move to contained/scoped
  sinks per the tier model.
- **#923** — make the test suite enforcement-clean: with the flag on,
  `test_dispatcher` fails and the broad suites hang, so CI cannot validate
  enable-time regressions. Must be green under `MIMIR_ACCESS_CONTROL_ENFORCED=1`
  before enabling.

---

## 8. Proposed work breakdown (design is settled — §6)

1. **Per-trigger capability config** (§5.1): manifest schema + a built-in profile
   for heartbeat/session-boundary; named capabilities validated against the tier
   table; narrow per-trigger roots from immutable operator config (not the global
   file-tool roots); manifest cannot self-grant/mutate its authority.
2. **Trust derivation** (§4): resolve content integrity from source identity —
   GitHub permission graph (collaborator/org/write), pointed-at JIRA instance,
   internal triggers — everything else untrusted; wholesale for trusted sources.
3. **`_get_allowed_sinks` → tier + trust gate** (§3, §5.2): replace the `#906`
   blanket poller block with the 2×2 (trust × blast-radius) deferring to
   containment policy; keep unbounded/exfil hard-blocked; add the Code-execution
   tier (worklink_run trusted-only; spawn_* blocked pending an isolation contract).
4. **Provenance schema + informational recall** (§5.3): `integrity`/`origin_trigger`/
   `origin_ref` immutable columns; render provenance on recall (grouped by trust)
   **without** tainting; enforcement taint from active ingests only.
5. **Network egress boundary** (§5.4): pollers no `fetch_url`; heartbeat
   approved-URL = egress-only (content untrusted); user ask-on-first-use per host;
   one boundary covering child processes (task-level egress control).
6. **opencode file-permission** for worklink (§5.5): set `external_directory` deny
   + restrict `bash`; verify `shell.sandbox` against our opencode version.
7. **Enable-time verification**: land the §7 blockers still open (#922 write-shell
   migration; #923 enforcement-clean suite), run the full suite under
   `MIMIR_ACCESS_CONTROL_ENFORCED=1` green, then the runbook in
   [`../authorization.md`](../authorization.md). (The other §7 review items —
   #1140–1144 — are already merged.)
