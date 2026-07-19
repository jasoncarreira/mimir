# Enforcement enablement: sink classification and poller capabilities

**Status:** DRAFT — revision 9. Incorporates mimir review rounds 1–7 (spawn and
`worklink_run` are not process-confined; the "scoped" file root is actually the
whole home; the `SAGA` category is too broad; integrity is an enablement
prerequisite; approved-URL authorizes the request, not the response bytes;
destination-allowlisting alone is insufficient for payload-bearing network sinks;
even exact-URL fetch leaks via invocation-pattern/redirects unless dispatch is
trusted-deterministic; integrity is a distinct axis needing its own executable
representation, not the confidentiality `ifc_state`; the gate must distinguish
active-ingest from informational-recall sources that share the accumulator) and
the operator's decisions
(per-trigger policy;
approved-URL = egress-only; trust the contributor / JIRA instance wholesale;
provenance schema; auto-recall must never handcuff a user turn; a heartbeat may
fetch its config-fixed approved URLs freely; enforcement-aware prompt guidance is
ergonomics-only; future isolation must run in docker **and** AWS ECS/Fargate → no
bubblewrap; low-bandwidth covert channels are an accepted residual). The open
design questions from rev 3 are resolved (§6).
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
     **egress authorization only** (which exact URLs may be fetched); the fetched
     **content stays untrusted** (§5.4). Approving an exact URL is not vouching for
     its bytes.
   - Everything else ingested from outside → **untrusted**.

The gate is the **2×2 of content-trust × sink blast-radius** (§3): *trusted →
any sink in the capability set; untrusted → Contained or Scoped-with-provenance
only; untrusted → Unbounded is blocked* (or explicit one-use declassification).
This is the integrity model **anchored on identity**. It replaces "trust the
turn because a trusted party started it," which does **not** survive the
confused-deputy case — untrusted content (an issue body, a web page, a comment)
folded into a trusted turn and then driving an action.

**Integrity is a distinct axis and needs its own executable representation — it is
NOT the existing `ifc_state`.** `ifc_state` today carries *confidentiality* labels
and ACLs, and it is **never "clean"**: every turn stamps its own `{private}`
confidentiality label at ingress, so "IFC empty/clean" is meaningless as an
integrity signal (mimir round 6). Represent integrity with **two fields on
`SourceLabel`**, both set server-side at the point a source enters:
- `integrity: trusted | untrusted` — from the trust rules above.
- `integrity_effect: active_ingest | informational` — **whether the source should
  gate actions**. `active_ingest` = the turn's own trigger content and live tool
  reads/fetches *this turn* (a fetched page, an unknown-author issue, an MCP
  result). `informational` = sources injected at prompt assembly that must inform
  but not gate — **auto-recalled memory (§5.3) and protected-prompt blocks**
  (recent-activity, identities, …).

Both `active_ingest` and `informational` sources ride the same `ifc_state`
accumulator (mimir round 7 — recalled/prompt sources already do), so the field is
what separates them. The **integrity gate fires iff an accumulated source is
`integrity == untrusted` AND `integrity_effect == active_ingest`.** An untrusted
*informational* source (a recalled untrusted memory) is rendered/visible but does
**not** gate — reconciling this gate with §5.3's "auto-recall never handcuffs a
user turn." Wherever this doc says "untrusted taint" / "the turn-taint gate," that
is the exact test — *any untrusted **active-ingest** source this turn* — never
confidentiality emptiness and never an informational recall.

### Per-trigger policy

| Trigger | Capability set (the ceiling) | Trust / gating |
|---|---|---|
| **Operator / user turn** | full (subject to admin tier) | operator's typed input is trusted; untrusted content read mid-turn is tainted → can't drive Unbounded sinks without one-use approval |
| **GitHub poller** | `worklink_run` (worktree + reviewed PR), scoped file/edit, read-only shell, `send_message` | **known contributor** (collaborator / org / write) → trusted → full code-work; **unknown author, or any comment by a non-contributor** → untrusted → **notify the operator only**, no autonomous action (operator then directs the agent) |
| **Research / RSS poller** | write memory (create atom + feedback/credit), scoped state file, scoped wiki, `send_message` — **no `fetch_url`, no `spawn`** | ingested web content is untrusted, but the capability set contains **no Unbounded sink**, so it is safe regardless — no per-author gating needed |
| **Heartbeat** | near-full incl. `fetch_url` from a **config-fixed approved-URL set** | internally triggered → trusted. A **deterministic** fetch of its config-fixed exact-URL set (any subset, repeatedly) is taint-independent — a prior fetch's untrusted content doesn't lock it out; **model-chosen** fetches instead fall under the turn-taint gate. Fetched **content stays untrusted**: drives scoped sinks (save state / wiki / memory) but not code/shell; non-approved destinations blocked; redirects re-checked per hop. Allowlist = exact URLs, not host wildcards (§5.4) |
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
- **Per-instance, not per-class:** each configured poller *instance* gets its own
  principal id + capability set + roots (not a shared `poller` principal), so two
  research pollers can't reach each other's state/memory and a grant is auditable
  to that instance. The instance principal + caps are **stable across
  continuations** — a job-complete/continuation turn resolves to the *same*
  instance principal, never widened or downgraded to a generic one.
- **One authoritative source, fail-closed, overrides can't widen:** `pollers.json`
  (skill pollers) and the built-in profiles (heartbeat, session-boundary) are the
  sole authority, with **deterministic precedence** — a manifest entry can only
  *narrow* a built-in profile, never widen it. Unknown authority-bearing values
  (capability name, tier, root) **fail closed**: the poller is rejected, not
  silently defaulted (distinct from today's fail-*safe* tuning parse).
  `pollers-overrides.yaml` stays **tuning-only** — authority-bearing fields are not
  in `POLLER_OVERRIDE_KEYS` and cannot be set there, and a capability grant is
  never `env`/`pass_env`-derived.
- **`operator_alert` capability:** the bounded notify-only sink — a single
  operator-configured alert destination that untrusted/notify-only triggers may
  send to (and nothing else), exempt from the `#906` block for that one
  destination only (§5.2).

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

Notify-only work (untrusted code, unknown-author GitHub) routes to the bounded
`operator_alert` destination (§5.1) — the **single** exception to the Unbounded
hard-block, scoped to that one fixed destination; every other cross-channel
destination stays blocked, and the exemption is a specific destination, not a
class of destinations.

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
- **Auto-recall** (relevance-based injection at prompt assembly) enters `ifc_state`
  as an `integrity_effect: informational` source (§4), so it renders its
  provenance tag but the integrity gate (which fires only on `active_ingest`
  sources) ignores it — no taint, no gating. A user turn stays fully able to work
  even if an untrusted memory is recalled into context.
- **Enforcement taint comes only from `active_ingest` sources** — the trigger's own
  content (poller payload, unknown-author issue) and live tool reads/fetches this
  turn — never from `informational` recalled/prompt sources. (This is exactly the
  distinction §4's `integrity_effect` field makes executable; without it, recalled
  sources riding the shared accumulator would wrongly trip the gate — mimir r7.)

The memory-poisoning defense is therefore: (1) **core memory is always blocked
and PR-gated — pre-existing and universal, for every principal, not
poller-specific** — so untrusted content never becomes an always-loaded trusted
instruction; (2) **provenance visibility** on recall so the agent down-weights
untrusted-origin facts; (3) **the action gate** on anything the turn actively
ingests.

**Accepted residual — cross-turn integrity laundering (decided: accept, revisit
later).** Because recall is `informational` and informational never gates, there
is a laundering chain: untrusted content stored on one turn → auto-recalled on a
*later* turn as `informational` → shapes a *trusted* turn's reasoning → that
trusted turn takes a privileged action it is itself authorized for. The recalled
fact never trips the gate (that's the point — it can't handcuff a user turn), but
it can still *influence* an authorized action. Provenance/down-weighting is not
an executable boundary here, so this path stays open. We **accept it** for the
initial enablement rather than gating recalled untrusted-origin content, which
would break the user-turn ergonomics that are a hard requirement. It is bounded
by: (1) core memory always blocked + PR-gated (untrusted content never becomes an
always-loaded instruction); (2) provenance visibility on recall; (3) the operator
as backstop on user turns; (4) tight per-trigger capability sets bounding
autonomous blast radius. **Revisit later** if it proves exploitable — the natural
future move is *turn-type-scoped* gating (keep user turns exempt; gate recalled
untrusted-origin content on autonomous turns, which have no human backstop). This
is a known limitation, not a closed hole.

### 5.4 Network egress: `fetch_url` and the application egress boundary

`fetch_url` / `web_search` / webhooks / `EXTERNAL_MCP` are where "let the agent
act" and "let data leak out" are the same action.

Network egress needs the destination allowlist **plus**, for some sinks, a check
on the request payload. Two sink shapes (mimir round 4):

- **URL-is-the-destination** — `fetch_url` against an **exact-URL** allowlist.
  Exact URLs keep data out of the URL *text*, but taint-independence needs more:
  the **choice** of which approved URLs to fetch, and their order/count/timing, is
  itself model-controllable and observable in the endpoints' logs — a low-bandwidth
  **covert channel** if the model picks fetches based on untrusted/sensitive
  content — and **redirects** can escape the exact URL. So:
  - **Trusted deterministic dispatch → taint-independent.** When the fetch set and
    order are fixed by config/trusted logic (a heartbeat monitoring a configured
    URL list each tick — its normal pattern), there is no model *choice* to encode
    data and no data in the URLs → fetch freely, repeatedly, regardless of turn
    taint. This satisfies "fetch approved URLs freely."
  - **Model-chosen fetches → model-controlled invocation → turn-taint gate.** If
    the model dynamically decides which/whether/order to fetch, that choice is
    treated like a model-emitted payload (rule below): allowed on a clean turn,
    gated once untrusted content has been ingested.
  - **Redirects constrained per hop** — an approved URL that 3xx-redirects must
    have each hop re-checked against the allowlist (or redirect-following
    disabled), else a redirect escapes the exact-URL bound.

  **Accepted residual:** covert channels are a bottomless well (timing, count,
  cache-state, resource usage, …). We take the cheap *structural* closes
  (deterministic dispatch + per-hop redirect check) and **consciously accept
  residual low-bandwidth covert channels** for the single-operator threat model
  rather than chase them indefinitely.
- **Payload-bearing** — `web_search` (query), `webhook` (body), external MCP
  (args), any child-process request. The destination is a *fixed/approved
  endpoint* (`_extract_sink_target` returns the Tavily URL, not the query; the
  webhook URL, not the body; the MCP tool name, not the args), while the payload
  is separate content that can carry data out to that approved destination — so
  destination-allowlisting is necessary but **not sufficient**.

  **Mechanism — payload-provenance, then turn-taint fallback.** Precise
  per-argument provenance is *not* achievable through an LLM: the model reads
  trusted and untrusted content together and emits a new string, so there is no
  reliable data-flow from an untrusted input to a specific query. Do not pretend
  to per-string taint tracking. Instead:
  1. **Trusted-by-construction payload → allowed** (regardless of turn taint). If
     the payload is server-supplied / config-derived — a heartbeat's configured
     search query, a fixed template — it is trusted by construction. Prefer this
     for autonomous payload-bearing egress: fill the payload from trusted config,
     don't let the trigger emit it free-form. (This is *why* exact-URL `fetch_url`
     removes the data-in-URL vector — the payload **is** the config-fixed URL;
     exact URLs still need trusted-deterministic dispatch + per-hop redirect checks
     for the invocation-pattern channel, per the URL-is-destination bullet above.)
  2. **Model-emitted payload → the integrity gate.** A free-form model-composed
     payload can't be proven clean, so it conservatively inherits the turn's
     **integrity** state: allowed only if the turn has ingested **no
     untrusted-integrity source** this turn (per §4 — *not* "IFC empty", which is
     never true), else blocked / one-use declassify. This is the **same
     integrity gate the action sinks use** — payload-bearing sinks join the action
     tier for model-emitted payloads; there is no separate per-payload machinery.

  So the only thing gated is a *model-composed* payload on a turn that has
  *already ingested* untrusted content — exactly the confused-deputy exfil. A
  heartbeat's config-driven searches and its exact-URL fetches are unaffected.
  (Coarse at turn granularity: it can over-block a genuinely-fine model-composed
  egress after an untrusted ingest — escape is the declassify, and doing trusted
  egress before ingesting untrusted content avoids it. Optional substring-matching
  against ingested bytes is defense-in-depth, never the control.)

The taint continues to gate *code/shell/action* sinks in all cases. By trigger:

- **GitHub / research pollers:** no `fetch_url` capability at all (they fetch via
  their own subprocess; the capability is simply not in their set).
- **Heartbeat:** `fetch_url` allowed against an **operator-approved allowlist** —
  authorization to reach those destinations, **not** a trust signal for the
  response (mimir: approving an exact URL authorizes the request, not the bytes).
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
- **User / operator turns:** **ask-on-first-use per exact URL** — the agent asks
  the first time it wants a destination, the operator approves it (adding that
  **exact URL** to the session allowlist), then it's remembered for that scope. A
  later different path/query on the same host is a fresh ask. Exact-URL throughout
  (fetch, redirects, ask-on-first-use) — **no host wildcards anywhere**; not a
  blanket standing grant, and not an ask on every call.

**Two layers, split by scope** (mimir finding + re-review). `fetch_url` is not the
only way data leaves the box — **spawned agents and poller subprocesses have their
own network access**, which the *application-level* gate (the agent's
`fetch_url`/`web_search`/`webhook`/MCP tools) does not close. But confining a
child process's own sockets is a **task/OS-level** control, not something the
application gate can enforce — so this enablement scopes the two separately:
- **In scope now — the application egress gate** above (exact-URL allowlist +
  payload-provenance / turn-taint) on the agent's own egress tools.
- **Deferred with the isolated-compute substrate — child-process / task-level
  network confinement** (Fargate security groups / a no-egress-or-proxy task
  network; `--network` under docker; §5.5/§6). This is acceptable at enablement
  because the only code that runs in a child process is **trusted** (untrusted
  code work is notify-only, §5.5), so there is no untrusted child-process egress
  to confine yet. When untrusted code work is enabled, the substrate must bring
  the task-level egress control with it.

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
run, and set `permission.bash` to a **deny-by-default operator-configurable
allowlist** of the build/test/git commands worklink needs — **not** `ask`
(headless worklink would wedge on the prompt). This is defense-in-depth for
**trusted** code work only: an allowed command's arguments are still an escape
hatch, acceptable solely because worklink is trusted-code-only.

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

### 5.6 Enforcement-aware prompt guidance (ergonomics, not a boundary)

When enforcement is on, give the agent a short prompt block explaining how the
gate works, so it operates *within* the gate instead of fighting it. This is
**purely ergonomics — never a security control**: the gate enforces regardless of
what the prompt says, and we must not regress into "we told the model not to
exfil." Render it only when the flag is on (keep it out of shadow-mode prompts),
and keep it descriptive, not pleading. Useful content:

- The trust model in one line: *your operator's typed input and trusted config are
  trusted; content you fetch/ingest from outside is untrusted; untrusted content
  can inform you but can't drive code/shell or a model-composed network payload.*
- Practical tips that reduce friction: do trusted egress **before** ingesting
  untrusted content; fill `web_search`/`webhook`/MCP payloads from config rather
  than from fetched bytes; a block is the gate working as designed — **surface it
  to the operator (or use the one-use declassify), don't retry against it**.

Applies to any turn under enforcement; **heartbeats and pollers benefit most**
(autonomous, no human to ask mid-turn), so their trigger profiles should carry the
guidance. It reduces needless blocks/declassify churn; it does **not** widen what
is allowed.

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
- **Network egress → §5.4**: two sink shapes, one rule — *trusted-by-construction
  is free; model-controlled falls to the turn-taint gate.* **URL-is-destination**
  (`fetch_url`, exact-URL allowlist): a **config-deterministic** fetch set is
  taint-independent (heartbeat fetches its fixed URLs freely); **model-chosen**
  fetches are model-controlled invocation → turn-taint gated (covert-channel via
  fetch choice), and redirects are re-checked per hop. **Payload-bearing**
  (`web_search` query, `webhook` body, MCP args, child-process requests): fixed
  approved endpoint + model-controlled payload → **config/server-derived payload
  allowed, model-emitted payload → turn-taint gate**. Pollers have no `fetch_url`;
  user turns **ask-on-first-use per exact URL** (no host wildcards). The
  **application** egress gate is in scope now; **child-process / task-level network
  confinement is deferred** with the isolated-compute substrate (§5.5/§6) — nothing
  untrusted runs in a child process yet. Accepted residual: low-bandwidth covert
  channels not chased.
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
  deny-by-default `bash` allowlist (works anywhere; no kernel deps).
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

1. **Per-trigger capability config** (§5.1): one authoritative schema (`pollers.json`
   + built-in profiles) with deterministic manifest-vs-built-in precedence and
   **fail-closed** handling of unknown authority-bearing values; named capabilities
   validated against the tier table; **per-instance** principals + narrow
   per-instance roots from immutable operator config (not the global file-tool
   roots); the `operator_alert` bounded sink; manifest/overrides cannot
   self-grant/mutate/widen authority (authority-bearing fields stay out of
   `POLLER_OVERRIDE_KEYS`).
2. **Integrity axis + trust derivation** (§4): add **two** fields to `SourceLabel`,
   both server-set at ingest — `integrity: trusted | untrusted` (from source
   identity: GitHub permission graph, pointed-at JIRA instance, internal triggers →
   trusted; else untrusted; wholesale for trusted sources) and `integrity_effect:
   active_ingest | informational` (active = trigger content + live tool reads/
   fetches; informational = auto-recall + protected-prompt blocks). A **distinct
   axis from the confidentiality labels `ifc_state` already carries** (never empty),
   riding the same source-accumulation.
3. **`_get_allowed_sinks` → tier + integrity gate** (§3, §5.2): replace the `#906`
   blanket poller block with the 2×2 (integrity × blast-radius) deferring to
   containment policy; the gate fires iff an accumulated source is
   `integrity == untrusted` **AND** `integrity_effect == active_ingest` (so
   informational recalls never gate — §5.3), not IFC emptiness; keep unbounded/exfil
   hard-blocked; add the Code-execution tier (worklink_run trusted-only; spawn_*
   blocked pending an isolation contract).
4. **Provenance schema + informational recall** (§5.3): `integrity`/`origin_trigger`/
   `origin_ref` immutable columns; render provenance on recall (grouped by trust)
   **without** tainting; enforcement taint from active ingests only.
5. **Application network-egress boundary** (§5.4): destination allowlist of
   **exact URLs** (no host wildcards anywhere — fetch, redirects, ask); URL-is-
   destination is taint-independent **only under trusted-deterministic dispatch**
   (config-fixed set/order) with **per-hop redirect checks** — model-chosen fetches
   fall to the integrity gate; **payload-bearing sinks** (`web_search`/`webhook`/
   MCP) allow config/server-derived payloads and integrity-gate model-emitted ones;
   pollers no `fetch_url`; user **ask-on-first-use per exact URL**. Child-process /
   task-level network confinement is **deferred** with the isolated-compute
   substrate (§5.5/§6) — trusted-only child code today means nothing untrusted to
   confine yet.
6. **opencode file-permission** for worklink (§5.5): set `external_directory` deny
   + `permission.bash` to a **deny-by-default operator-configurable allowlist**
   (not `ask` — headless wedges); verify `shell.sandbox` against our opencode
   version. Defense-in-depth for trusted code work only, not a confinement proof.
6a. **Enforcement-aware prompt guidance** (§5.6): a flag-gated prompt block +
   heartbeat/poller profile guidance describing the trust/taint model — ergonomics
   only, never a boundary.
7. **Enable-time verification**: land the §7 blockers still open (#922 write-shell
   migration; #923 enforcement-clean suite), run the full suite under
   `MIMIR_ACCESS_CONTROL_ENFORCED=1` green, then the runbook in
   [`../authorization.md`](../authorization.md). (The other §7 review items —
   #1140–1144 — are already merged.)
