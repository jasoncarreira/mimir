# Enforcement enablement: sink classification and poller capabilities

**Status:** DRAFT design proposal — seeking review (mimir + operator).
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

- **GitHub poller code work** = `worklink_run` / `spawn_*` running in an
  **isolated worktree**, whose output is a **PR a human reviews** before it's
  real (plus the staged-diff secret-scan added in #1137). Untrusted issue
  content driving that is safe *because the blast radius is a sandbox + a review
  gate* — not because the content is trusted. This covers both greenfield and
  "update/test from an existing review."
- **Research poller findings** = `write_file` to a **scoped state root** (core
  memory and system paths already un-writable) and a memory write to the normal
  recallable store **tagged with untrusted provenance** (see §5.3).

So the rule becomes: **"untrusted-payload turn → allow sinks whose containment
bounds the blast radius; hard-block only the unbounded / exfiltrating ones."**
The containment policies already exist on the principal — `#906` just needs to
*defer to them* instead of pre-empting them.

---

## 3. Sink tiers

| Tier | Sinks | Why it's safe / not | Untrusted-payload turn |
|---|---|---|---|
| **Contained** | `worklink_run`, `spawn_*` (worktree), `write_file`/`edit_file` (scoped roots), read-only `shell` | sandboxed / scoped / human-reviewed / reversible; blast radius is bounded | **Allow** if the destination satisfies the containment policy |
| **Scoped-with-provenance** | `memory_store` / `saga_*` writes to the recallable store | usable later, but carries where it came from; can't reach core memory | **Allow** into the recallable store, **tagged untrusted** (never core memory) |
| **Unbounded / exfiltrating** | `fetch_url` / `NETWORK`, webhooks, `EXTERNAL_MCP`; write-`shell` on the live host; writes to core memory / prompts / system paths | leaves the trust boundary or is irreversible/self-modifying | **Hard-block** (this is where `#906` still earns its keep) |

---

## 4. Turn model

The tiers apply to every turn; who gets what differs by turn kind.

| | **Contained actions** (worktree spawn/worklink, scoped file/edit, read-only shell) | **Memory/state write** | **`fetch_url` / network exfil** |
|---|---|---|---|
| **User / operator turn** | allowed (operator-trust) | allowed, provenance-tagged | **ask permission** (§5.4) |
| **Poller turn** | allowed **per declared capability** | allowed **per declared capability**, provenance-tagged | **blocked** |
| **Any turn** | core-memory / system-path / live-host write-`shell` → hard-blocked | — | hard-blocked |

Rationale:
- The **operator** on their own interactive turn is trusted to drive contained
  actions (this matches the accepted single-operator shell posture; see the open
  question in §6 about multi-user).
- A **poller** ingesting untrusted content gets exactly the contained sinks its
  manifest declares, and no exfil path (there is no human present to approve
  one, and pollers fetch via their own subprocess anyway).

---

## 5. Design

### 5.1 Per-poller capabilities in the manifest

Replace the one-size-fits-all `poller` principal with **per-poller capability
declarations in the poller's config** (`pollers.json` manifest). Each poller
declares the capability set it needs; the set is validated against the tier
table at discovery time (a manifest cannot grant itself an Unbounded-tier sink).

- GitHub poller → declares code-work (`worklink_run`, `spawn_*`, scoped
  file/edit, read-only shell).
- Research / RSS poller → declares `write_file` (scoped) + `memory_store`
  (recallable, provenance-tagged).
- A pure notifier poller → declares nothing beyond `send_message`.

This also fixes the "research pollers can't write memory" gap: memory-write
becomes a declarable capability, not an ungranted one.

### 5.2 `#906` becomes tier-based (defer to containment)

In `_get_allowed_sinks`, the `poller_payload` branch stops being a blanket
`return frozenset()`. Instead: for a `poller_payload` turn, a sink is allowed
iff (a) it is in the poller's declared capabilities, (b) it is a Contained or
Scoped-with-provenance tier sink, and (c) the requested destination satisfies
the sink's containment policy (verified, not asserted). Unbounded-tier sinks
stay hard-blocked. This keeps the `#906` guarantee for the sinks that matter
while unblocking contained work.

### 5.3 Provenance tags on untrusted-derived writes (and why **not** a quarantine namespace)

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

### 5.4 `fetch_url`: blocked on pollers; permission-ask on user turns

`fetch_url` / `web_search` / webhooks / `EXTERNAL_MCP` are the one place where
"let the agent act" and "let data leak out" are the same action, so they stay
Unbounded-tier:

- **Poller turns:** hard-blocked. Pollers fetch via their own subprocess, so the
  agent's `fetch_url` is never needed for their work.
- **User turns:** instead of blanket-allow, the agent **asks permission** — a
  small human-in-the-loop escalation (agent requests the fetch, operator
  approves, then it egresses). See open question §6.2 for the exact flow.

### 5.5 GitHub poller: greenfield **and** review/update/test

Both "develop new code" and "update/test from an existing code review" run in an
isolated worktree with a reviewed PR as the only durable output. `worklink_run`
/ `spawn_*` cover acting on an existing review, not just greenfield — so no
separate mechanism is needed; the review path is the same contained shape.

---

## 6. Open questions (for reviewers — mimir and operator)

1. **Capability config schema.** Should a poller manifest declare **named
   capabilities** (e.g. `["worklink_run", "write_file", "memory_store"]`,
   validated against the tier table) or coarse **roles** (e.g. `code-work`,
   `research-save`)? Preference: named capabilities validated against tiers, so
   a manifest can't self-grant outside its tier — but roles are simpler to
   author. Which?

2. **The `fetch_url` "ask permission" flow.** Two shapes: (a) a **mid-turn
   prompt back to the operator** in the channel ("I need to fetch `<url>`, ok?")
   that blocks until answered — most secure, per-call; or (b) a **standing /
   allowlist grant** the operator pre-approves (host or URL patterns) — less
   interrupting, coarser. Which, or a hybrid (ask once per host, remember)?

3. **Operator-trust scope for user turns (single- vs multi-user).** The model
   above trusts the operator's own interactive turns to drive contained actions.
   That is right for single-operator mimirbot. Its residual gap: untrusted
   content pulled into an operator turn (a shared-channel message, a forwarded
   payload) could drive a contained action, because an ACL encodes *who may
   see*, not *is trusted to act*. Before mimir ever faces untrusted multi-user
   chat, this wants a genuine **integrity dimension** on source labels
   (trusted = operator direct input; untrusted = external/inbound) that gates
   action sinks regardless of visibility. Do we accept operator-trust now and
   track the integrity-label work as the multi-user prerequisite, or build the
   integrity dimension up front?

4. **Provenance surfacing on recall.** How should untrusted-source provenance be
   rendered when a finding is recalled into the prompt — an inline tag per
   memory, a grouped "from untrusted sources" section, or a structured field the
   agent is prompted to weigh? (Affects whether the tag actually changes
   behavior vs. being decorative.)

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
