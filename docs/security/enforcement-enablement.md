# Enforcement enablement: sink classification and poller capabilities

**Status:** DRAFT — revision 2. Incorporates mimir review round 1 (spawn is not
contained; the "scoped" file root is actually the whole home; the `SAGA` category
is too broad; integrity is an enablement prerequisite) and the operator's
per-trigger policy (GitHub known-contributor vs unknown; research pollers;
heartbeats). Seeking another review round.
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

- **GitHub poller code work** = `worklink_run` running in an **isolated
  worktree** (`create_isolated_checkout`), whose output is a **PR a human
  reviews** before it's real, after `observe_evidence` validates the diff/tests
  (plus the staged-diff secret-scan added in #1137). Untrusted issue content
  driving *that* is safe *because the blast radius is a sandbox + a review gate*
  — not because the content is trusted. This covers both greenfield and
  "update/test from an existing review."
- **Research poller findings** = `write_file` to a **scoped state root** (core
  memory and system paths already un-writable) and a memory write to the normal
  recallable store **tagged with untrusted provenance** (see §5.3).

> **Important correction (mimir review):** generic `spawn_*`
> (`spawn_open_code` / `spawn_codex` / `spawn_claude_code`) is **not** contained
> the way `worklink_run` is. It runs `opencode run --dir <cwd>` as an ordinary
> subprocess; its only confinement is the `spawn_workspace` policy
> (`_target_within_configured_write_roots`), which permits **all of
> `MIMIR_HOME` plus configured RW roots** (e.g. `/workspace/mimir`) and does
> **not** apply the file-tool read-only guard or any PR-only-output
> postcondition. So `spawn_*` is an **unbounded host-write / code-execution
> sink**, not a contained one. Only `worklink_run` has the
> isolated-worktree + evidence + review shape. This doc's tiers reflect that:
> **route all poller code work through `worklink_run`; keep generic `spawn_*`
> blocked on untrusted-payload turns** until it has an executable isolation
> contract (§5.5).

So the rule becomes: **"untrusted-payload turn → allow sinks whose containment
bounds the blast radius; hard-block only the unbounded / exfiltrating ones."**
The containment policies already exist on the principal — `#906` just needs to
*defer to them* instead of pre-empting them.

---

## 3. Sink tiers

| Tier | Sinks | Why it's safe / not | Untrusted-payload turn |
|---|---|---|---|
| **Contained** | `worklink_run` (isolated worktree + evidence + reviewed PR), `write_file`/`edit_file` (scoped roots), read-only `shell` | sandboxed / scoped / human-reviewed / reversible; blast radius is bounded | **Allow** if the destination satisfies the containment policy |
| **Scoped-with-provenance** | `memory_store` / `saga_*` writes to the recallable store | usable later, but carries where it came from; can't reach core memory | **Allow** into the recallable store, **tagged untrusted** (never core memory) |
| **Unbounded / exfiltrating** | generic `spawn_*` (ordinary subprocess, writes anywhere in the RW roots, no review gate); `fetch_url` / `NETWORK`, webhooks, `EXTERNAL_MCP`; write-`shell` on the live host; writes to core memory / prompts / system paths | leaves the trust boundary, executes attacker-influenced code with host write, or is irreversible/self-modifying | **Hard-block** (this is where `#906` still earns its keep). `spawn_*` moves to **Contained** only once it has the isolation contract in §5.5. |

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
     as a whole. Content from **anyone else — including *comments* on an
     otherwise-trusted PR — is untrusted**.
   - **`fetch_url` from an operator-approved URL** (heartbeat) → **trusted**
     (§5.4 — an explicit, accepted trade-off).
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
| **Research / RSS poller** | write memory (create atom + feedback/credit), scoped state file, `send_message` — **no `fetch_url`, no `spawn`** | ingested web content is untrusted, but the capability set contains **no Unbounded sink**, so it is safe regardless — no per-author gating needed |
| **Heartbeat** | near-full incl. `fetch_url` from an **approved-URL list** | internally triggered → trusted; approved-URL content is trusted (§5.4) |
| **Session-boundary turn** | session-boundary writes | internal → trusted |
| **(future) JIRA poller** | write chainlinks, update docs (scoped), write memory | trust source TBD (JIRA identity); declared like any other trigger |

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
their origin (which poller, which source, untrusted). We deliberately do **not**
route them to a separate "quarantine" namespace, because a quarantine only has
value if something downstream reads it differently — and in mimir, recalled
memory just flows into the prompt as context. A quarantine namespace without a
down-weighting consumer is either recalled (exactly as dangerous as
un-quarantined) or never recalled (wasted). The real defense is the pairing that
already (mostly) exists:

- **core memory stays un-writable by pollers** (already enforced) — untrusted
  content never reaches the always-loaded, always-trusted set;
- **provenance rides on the write** — on recall, "from an untrusted web fetch
  via research-poller X" is visible in context for the agent (and operator) to
  weigh;
- **the action gate** — a poisoned finding that gets recalled still cannot
  *drive* a privileged action without passing this same sink classification.

### 5.4 Network egress: `fetch_url` and the uniform egress boundary

`fetch_url` / `web_search` / webhooks / `EXTERNAL_MCP` are where "let the agent
act" and "let data leak out" are the same action. By trigger:

- **GitHub / research pollers:** no `fetch_url` capability at all (they fetch via
  their own subprocess; the capability is simply not in their set).
- **Heartbeat:** `fetch_url` allowed against an **operator-approved URL/host
  list**, and content from an approved URL is treated as **trusted** (not
  tainted). This is an explicit trade-off: tainting approved-URL content would
  make the heartbeat unable to do its work, so approving a URL *is* vouching for
  it. Accepted residual: a compromised approved site could feed the heartbeat.
- **User / operator turns:** **exact-host/URL grant with ask-on-first-use**
  (mimir rec) — the agent asks the first time it wants a host, the operator
  approves that host/URL, and it's remembered for that scope. Not a blanket
  standing network grant, and not an ask on literally every call.

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

`spawn_*` moves out of the Unbounded tier only once it has an **executable
isolation contract**: a freshly-created isolated worktree, enforced path
confinement of *all* writable outputs (not the operator-wide RW roots),
secret/environment minimization, egress confinement (§5.4), diff validation, and
PR/review-only publication of durable effects — i.e. it must earn the same
postconditions `worklink_run` already has, verifiably, before untrusted-payload
turns may drive it.

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
- **Network egress → §5.4**: pollers have no `fetch_url`; heartbeat uses the
  approved-URL list (approved content trusted); user turns ask-on-first-use per
  host; one **uniform egress boundary including child processes**, not a per-tool
  special case.
- **Memory tier → scoped ops, not the whole category** (§5.3): create-atom +
  feedback/credit, provenance-tagged; no `saga_forget` / session-boundary.
- **Provenance → rendered structurally, never an enforcement boundary** (mimir
  rec): the tag informs; the action gate enforces.

**Remaining open:**

1. **Mixed / embedded content inside a trusted PR.** Top-level comments are
   handled (a non-contributor comment is untrusted → notify). But a known
   contributor's PR built on an unknown fork, or quoting external material —
   where exactly is the taint boundary inside an otherwise-trusted item?
2. **Provenance schema + recall rendering.** The exact immutable origin/integrity
   metadata stored on a write, and how it surfaces on recall (inline per-atom vs
   grouped) so it actually informs weighting rather than being decorative.
3. **Future trigger trust sources (e.g. JIRA).** How a JIRA ticket's author trust
   is derived (project role? reporter identity?) — needed before that poller
   lands, not now.
4. **`spawn_*` isolation contract — build it, or Worklink-only indefinitely?**
   §5.5 specifies what it would take; the call is whether to invest in a
   contained `spawn_*` or keep all untrusted code work on `worklink_run`.

---

## 7. Other enablement blockers (from the review — on the path, tracked separately)

These are not part of the sink-classification design but are on the critical
path to flipping the flag; captured here so the enablement picture is complete:

- **`attempted_service` fail-open (security bug).** A non-admin who POSTs
  `/event` with `trigger="poller"` skips the IFC sink gate for OPEN tools
  (`fetch_url`/`web_search` become allowed). Verified deny→allow. Fix: run the
  sink gate for OPEN operations regardless of `attempted_service`, and/or reject
  service triggers from non-service identities at ingress.
- **`shell_job_complete` continuation lockout.** The continuation event is built
  with `author=None`, `source="system"`, and no `service_principal`, so under
  enforce it is denied *every* tool including its own same-channel reply. The
  `bash_async` notify/continue workflow breaks. Fix: give the continuation a
  registered service principal (or inherit the originating turn's context).
- **#922** — trusted-service autonomous maintenance off raw write-shell (the
  scheduler/poller shell profile is read-only; write-shell maintenance turns
  break on enable). Overlaps with §5: those turns should move to
  contained/scoped sinks.
- **#923** — the test suite is not enforcement-clean: with the flag on,
  `test_dispatcher` fails and the broad suites hang, so CI cannot validate
  enable-time regressions. Must be green under `MIMIR_ACCESS_CONTROL_ENFORCED=1`
  before enabling.
- **R1** — `protected_prompt` sources are not channel-bound in `_get_allowed_sinks`
  (only ACL-checked), a potential cross-channel egress; bind them like the other
  source kinds.
- **R2** — `InformationFlowState.merge` label-monotonicity has a masked-test gap
  (reverting the union to keep only `added` fails 0 tests); add a regression that
  merges a tainted `current` with a clean `added` and asserts taint survives.

---

## 8. Proposed work breakdown (once the design is settled)

1. Poller capability config: manifest schema + parsing/validation against the
   tier table (§5.1).
2. `_get_allowed_sinks`: `#906` → tier-based deferral to containment policy
   (§5.2); add memory-write as a Scoped-with-provenance tier sink.
3. Provenance tags on untrusted-derived writes + recall surfacing (§5.3, Q4).
4. User-turn operator-trust allowance for Contained/Scoped sinks (§4).
5. `fetch_url` permission-ask flow (§5.4, Q2).
6. Fixes for §7 (fail-open, continuation lockout, R1, R2) + make the suite
   enforcement-clean (#923).
7. Enable-time verification: run the full suite under
   `MIMIR_ACCESS_CONTROL_ENFORCED=1` green, then the runbook in
   [`../authorization.md`](../authorization.md).
