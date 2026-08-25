# Enforcement enablement: sink classification and poller capabilities

**Status:** DRAFT — revision 10. Incorporates mimir review rounds 1–7 (spawn and
`worklink_run` are not process-confined; the "scoped" file root is actually the
whole home; the `SAGA` category is too broad; integrity is an enablement
prerequisite; approved-URL authorizes the request, not the response bytes;
destination-allowlisting alone is insufficient for audience-bearing network sinks;
exact-URL fetches re-check redirects and accept the low-bandwidth invocation-pattern
residual; integrity is a distinct axis needing its own executable
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
**Date:** 2026-07-25.
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
- **Code work** = `worklink_run`. It has an isolated Git clone checkout
  (`create_isolated_checkout`), `observe_evidence` diff/test validation, and a
  reviewed-PR-only durable output — so its **git/review blast radius** is
  bounded. This is safe for **trusted** code work (a known contributor's PR,
  your own request, a heartbeat).

> **Important correction (mimir review, rounds 1–2):** "code work" is **not
> process-confined**, so it is safe only for **trusted** content — not for
> untrusted payloads.
> - Generic `spawn_open_code`
>   isn't even git/review-contained: it runs the CLI as an ordinary subprocess
>   whose only confinement is `spawn_workspace`
>   (`_target_within_configured_write_roots` = **all of `MIMIR_HOME` + configured
>   RW roots**), with no read-only guard and no PR postcondition.
> - `worklink_run`'s checkout is only a **`cwd`**, not a sandbox. Its compute
>   (`LocalSubprocessComputeBackend`) reports `shared_filesystem=True,
>   network_isolated=False` and launches the CLI with `HOME` + provider creds —
>   so the child can **write outside the checkout and reach the network freely**;
>   the diff-review only inspects the checkout's git diff, not process side
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
| **Unbounded / exfiltrating** | destination-safe `fetch_url` / `web_search`; audience-bearing webhooks / `http_request`; `EXTERNAL_MCP`; write-`shell` on the live host; writes to core memory / prompts / system paths | leaves the trust boundary or is irreversible/self-modifying | **Destination-safe egress:** allow only through exact-URL / fixed-service controls (§5.4). **Audience-bearing / MCP / other unbounded sinks:** hard-block (`#906`) | Allow only via the egress boundary (heartbeat approved-URL list; fixed search service; user ask-on-first-use) — §5.4 |

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
   - **GitHub content** → trusted iff GitHub's content API identifies the author
      and that login is a **repo collaborator or active org member** according to
      the server-side permission API (GitHub's own relationship graph — operator-
      controlled, un-forgeable by a PR or poller payload). Such a contributor's issue/PR is trusted
     **as a whole**, including material it embeds/quotes or is built on top of —
     we trust the contributor not to introduce malicious content (operator
     decision). The only untrusted github content is from **non-contributors**
     (unknown authors, comments by non-contributors) → untrusted.
   - **A trusted external system we point at** (e.g. a JIRA instance) → trusted,
     on the basis that its admins gate who can file/assign and won't route
     untrusted issues to us (operator decision). Declared per trigger like any
     other trust source.
    - **Fetched page content** → untrusted active ingest, including content from
       an operator-approved URL. `approved_fetch_urls()` authorizes exact-URL GET
       egress and redirects; it is not a trust grant for returned bytes (#1139).
    - **Mimir's own context** (`<home>/.mimir_builtin_skills/**`,
       `<home>/skills/**`, `<home>/memory/**`, `<home>/state/**`, and framework-
       preloaded self-authored prompt blocks) → trusted informational, except
       `<home>/state/pollers/**`, which poller subprocesses can populate from
       external events and which remains untrusted active ingest. Filesystem
       classification resolves both the server-configured home and requested path
       before checking containment; prefix lookalikes and symlink escapes are not
       trusted. Successful model writes to memory/state are stamped in protected
       `.mimir` metadata with the least trust of the live server-owned carrier. A
       later read of an untrusted-derived file therefore remains untrusted active
       ingest instead of laundering taint through a self-authored-looking path.
       Prior assistant history stores the same server-derived integrity at send
       time and reloads as informational; clean self-authored history is trusted,
       while output produced from untrusted input remains untrusted informational.
       Framework-preloaded identities, prompts, session state, and other context
       assembled from server-owned state are trusted informational. Resolved paths
       and framework constructors are the evidence; caller metadata, model output,
       and model-supplied parameters cannot choose these labels.
   - **An operator-configured MCP tool** → use that exact tool policy's explicit
     `result_integrity` grant. `trusted` vouches for successful returned content;
     `untrusted` retains untrusted active ingest. Server locality, transport,
     display name, operation labels such as “read-only,” model arguments, and MCP
     response fields are not trust signals.
   - **A GitHub poller's framework-authored remediation trigger** → trusted only
     for the closed remediation event set and only after the server re-fetches the
     open PR and matches its number, URL, configured self author, repository,
     head/base refs, and head/base SHAs. The event name or metadata emitted by the
     poller subprocess is never sufficient by itself. PR bodies and comments,
     fetched pages, and attachments retain their independently derived trust.
   - Everything else ingested from outside → **untrusted**.

The gate is the **2×2 of content-trust × sink blast-radius** (§3): *trusted →
any sink in the capability set; untrusted → Contained or Scoped-with-provenance
only; untrusted → audience-bearing or otherwise Unbounded sinks are blocked*
(or explicit one-use declassification). Destination-safe `fetch_url` / `web_search`
are the narrow exception: exact-URL / fixed-service reachability is their control (§5.4).
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
  but not gate — **auto-recalled memory (§5.3) and framework-authored protected-prompt blocks**
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

The remediation trigger itself is trusted active ingest, not external content.
This is the “trigger payload is not untrusted ingest” fix; `repo_test` remains a
FORGE sink with the repository family. Successful `repo_checkout` and `repo_test`
results still add `protected_tool` repository provenance as untrusted
*informational* sources. Such labels can appear on later `repo_test` or
`pr_submit_review` audits, but do not taint the turn; cross-PR/head repository
provenance and execution-fault active ingest remain blocked.

#### 2026-07-29 interactive shadow re-baseline (#1054)

The local `MIMIR_HOME/logs/events.jsonl` audit contained 699
`shadow_tool_decision` records on 2026-07-29 with `trigger=user_message` and
`would_block=true`: 510 `shell_exec`, 112 `edit_file`, 45 `send_message`, 17
`bash_async`, 12 `write_file`, two `react`, and one
`saga_record_skill_learning`. This is a later, still-growing corpus than the
issue's 649/day snapshot, so raw totals are not directly comparable.

Replaying the 15 completed interactive turn traces in `turns.jsonl` under the
narrower source rules projects 14 newly permitted early message sinks and 661
remaining denials out of 675 recorded sink attempts. The other 24 shadow
denials had no completed turn trace and are conservatively retained, producing
a projected current-corpus count of **685**, 14 fewer than the same corpus's
699 and 36 more than the earlier 649 snapshot because the live corpus grew.
The completed-trace residue is 612 attempts after an active read of non-Mimir
filesystem content and 49 after async shell output; the 24 incomplete-trace
attempts remain unclassified. No genuinely untrusted source was reclassified to
obtain this reduction.

### Per-trigger policy

| Trigger | Capability set (the ceiling) | Trust / gating |
|---|---|---|
| **Operator / user turn** | full (subject to admin tier) | operator's typed input is trusted; untrusted content read mid-turn is tainted → can't drive Unbounded sinks without one-use approval |
| **GitHub poller** | `worklink_run` (isolated checkout + reviewed PR), scoped file/edit, read-only shell, `send_message` | **known contributor** (collaborator / org member) → trusted → full code-work; **unknown author, or any comment by a non-contributor** → untrusted → **notify the operator only**, no autonomous action (operator then directs the agent) |
| **Research / RSS poller** | write memory (create atom + feedback/credit), scoped state file, scoped wiki, `send_message` — **no `fetch_url`, no `spawn`** | ingested web content is untrusted, but the capability set contains **no Unbounded sink**, so it is safe regardless — no per-author gating needed |
| **Heartbeat** | near-full incl. `fetch_url` from an **approved URL set** and `web_search` through its fixed service | internally triggered → trusted. Exact/fixed destination egress is taint-independent, but fetched content is always untrusted active ingest. Scope-only fetches require a clean turn; non-approved destinations are blocked; redirects are re-checked per hop (§5.4). |
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
able to write another trigger's authority/config. Repo-working pollers are the
explicit exception for configured external repository roots, as described below.

**Repository-review execution profile.** A poller using the built-in `github`
authority profile is explicitly repo-working. Its `shell_exec` and `bash_async`
sinks use `shell_profile=repo_review`, while research and custom pollers retain
`scheduler_read_only`. `repo_review` is a command-shape allow-list for the
operations observed in review turns: bounded `gh pr view/diff/checks`, Git
status/log/diff/fetch/checkout, `npm ci --ignore-scripts`, `npm test`/`npm run
test`, and pytest (directly or as `uv run pytest`) with a narrow selection and
reporting option allow-list plus relative collection paths. It excludes pytest
plugin/config/debugger controls such as `-p`, `-c`, `-o`, `--rootdir`, and
`--pdb`, rejects response files and absolute/traversing collection paths, and scrubs
`PYTEST_ADDOPTS`/`PYTEST_PLUGINS` for direct execution. It does not admit shell
launchers, arbitrary interpreters, `spawn_*`, `rm`, Git push/history/config
mutation, GitHub credential mutation, or open-ended package install/update
commands. `npm ci --ignore-scripts` is the one declared dependency-materialization
command because a clean install is part of the repository test contract; lifecycle
scripts and other network-installing package operations remain denied.

**No profile admits `--jq`.** `gh` evaluates the filter in-process, and jq's
`env` and `$ENV` builtins return the process environment — which
`direct_exec_env` copies wholesale from the parent, credentials included. So
`gh pr list --json number --jq env` was an *admitted* command that printed
`DISCORD_TOKEN`, `GITHUB_TOKEN`, `GPG_KEY`, `MIMIR_API_KEY` and the provider keys
into the tool result, and from there into the model's context and the turn
transcript. Enforcement was not a mitigation, because the command was allowed
rather than merely unblocked. The option is removed from every profile's
allow-list; callers pass `--json <fields>` and filter the result themselves.
Removing it costs nothing, since every non-degenerate filter was already refused
by the metacharacter scan (`|`, `[`, `]` and `{` never reach `shlex`). Do not
reintroduce it with `env` blocklisted: that is a denylist over an expression
language, and the next builtin reaching process state reopens the hole. The same
question applies to any future option that evaluates a caller-supplied
expression — `--template` is retained only because gh's template function set is
fixed and exposes no environment accessor.

**Scheduled-maintenance execution profile.** Static `scheduled_tick` services and
the built-in heartbeat authority use `shell_profile=maintenance`; GitHub pollers
remain on `repo_review`, research/custom pollers remain on
`scheduler_read_only`, and upgrades remain on `upgrade_workspace`. The profile
adds only command-shape allow-lists for maintenance inspection: Git
`status`/`branch --show-current`/`log`/`diff`/`show`, each requiring
`git -C <configured-root> ...` so the repository target is explicit in the
authorized argv (bare Git commands are deliberately denied), including the
workspace-scoped landed-fix check in
`mimir/prompt_templates/issues-audit.md:48` and the prompt-common `git log
--oneline -<n>` shorthand), GitHub PR/issue `list` and `view`, and Chainlink
issue `list`, `ready`, and `show`. The pinned operator-installed absolute
Chainlink path is accepted only at that exact regular-file location; bare or other
model-supplied executable paths remain denied. These support the read/inspect
work called for by
heartbeat's backlog and state-management workflow
(`mimir/prompt_templates/heartbeat.md:121-179`), reflection's repository and
issue follow-up review (`mimir/prompt_templates/reflect.md:97-153`), memory
hygiene's bounded follow-up triage
(`mimir/prompt_templates/memory-hygiene.md:62-84`), and the issues audit's fix
verification (`mimir/prompt_templates/issues-audit.md:38-64`).

`maintenance` deliberately does not authorize the mutating examples in those
prompts. Chainlink create/comment, file deletion, Git commit/push/history/config
mutation, GitHub mutation or authentication, package installation, test runners,
arbitrary interpreters, shell launchers, and `spawn_*` remain denied. Maintenance
Git execution also resolves `-C` within `MIMIR_HOME` or an explicit
`MIMIR_FILE_TOOL_ROOTS` entry (the implicit `/tmp` file-tool convenience root is
not included), injects `-C` even when the model omitted it, and binds execution
to a no-pager/config-neutralized argv. Every admitted Git argv includes
`-c core.fsmonitor= -c core.hooksPath=/dev/null -c diff.external=
-c protocol.allow=never --no-pager --no-optional-locks`; diff-producing
subcommands also receive `--no-ext-diff --no-textconv`. The direct-exec
sandbox scrubs inherited `GIT_*` repository/config/helper selectors and disables
system config plus optional locks. Configured clean/smudge/process
filter driver names are enumerated before admission and neutralized with
per-driver `-c filter.<driver>.<kind>=` overrides. This prevents repo or global
Git configuration and checkout attributes from launching fsmonitor,
external-diff, textconv, or content-filter helpers. Verbose `git status` is
excluded because it renders a diff; admitted status forms and `branch` do not
accept the two diff flags. Like every shell profile, it inherits
control-character rejection,
`shlex` parsing, leading-tilde rejection, and the requirement that the admitted
argv itself is the execution artifact.

The profile does **not** make the shipped prompts' shell snippets fully
compatible. The production corpus measured during PR #1188 review contained
10,140 maintenance shell occurrences, of which only 100 were admitted at the
reviewed head: 99% used pipelines, redirects, `&&`, or globs rejected by the
shared control-character guard. That guard remains intentionally unchanged.
The architecture decision for this slice is prompt migration to structured file
and purpose-built tools, not a bounded-pipeline parser hidden in an allow-list
PR; `cat`/`head`/`tail`/`sed` remain out of this profile. The shipped maintenance
prompts now state that boundary explicitly; memory hygiene replaces its
arbitrary-interpreter scan, issues audit replaces its glob/sort pipeline,
reflection replaces its command substitution, redirect, `tail | jq`, and
rotation shell snippets, and heartbeat no longer recommends jq pipelines or
committing from the maintenance turn. Existing
operator-customized prompt copies still need the same migration. Therefore the
original "empty `service_sink_destination_denied` class" acceptance criterion is
not a valid success metric for existing free-form prompt output. Re-baseline that
telemetry after prompt migration before treating the class as a regression.
File-write scope for dynamic principals is likewise a separate policy concern and
is not widened by this profile.

Service principals freeze the explicit `:rw` entries from
`MIMIR_FILE_TOOL_ROOTS` into their file-sink roots alongside their declared
trigger roots and the safe portions of `<home>/state` and `<home>/memory`. The
shared protected-path check rejects paths outside those roots, read-only
configured roots, relative dynamic targets, symlink escapes, and the protected
write surfaces from #970 (`.env`, configuration, credentials, identities,
secrets, prompts, and memory core). It also rejects every `.git` path component,
case-insensitively and with trailing-dot variants denied, on both the lexical and
resolved path. This prevents service writes to hooks, repository configuration,
and other executable Git metadata, including through a symlink into `.git` or a
`.git` symlink out of an admitted root. `.gitignore` and `.gitattributes` remain
admitted: neither is Git metadata, and attributes can select a diff driver but
cannot define its executable command. Human/admin write authority is unchanged.

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

Network egress is split by **destination reachability**, not merely by whether
some request field looks like a payload:

- **Destination-safe egress is taint-independent.** `fetch_url` carries no
  model-controlled data out: the destination must match an exact approved URL,
  the request is GET-only, there is no model-supplied body or arbitrary header,
  and every redirect hop is re-checked against the same allowlist. `web_search` is
  a deliberate, distinct exception: its model-composed query is an accepted
  outbound channel because it reaches only one operator-fixed trusted service.
  Its results remain untrusted active ingest, which taints its own turn; gating
  the query would therefore cap every research turn at one search, so gating is
  not an available option. The `fetch_url` trailing-`/*` mitigation does not
  transfer: search has no model-chosen destination to narrow to an exact URL,
  only a model-chosen query to the fixed endpoint. This acceptance, like the
  trusted-operator `shell_exec` allowance in `access_control.py`, is limited to
  the current single-operator posture. It is unsafe under untrusted multi-user
  chat, where an attacker could induce a tainted turn to encode data into a
  query. Search result labelling is unchanged: trusting the service transport
  does not trust the third-party content it returns. The choice/order/timing of
  approved fetches is a low-bandwidth invocation-pattern channel that this
  single-operator threat model also explicitly accepts.
- **Audience-bearing egress stays behind the turn-taint gate.** `webhook` and
  `http_request` can send a free-form model body to an approved URL that may be
  a human or multi-party audience, so exact destination approval is necessary
  but not sufficient. External MCP (`mcp_*`) uses the explicit per-tool posture
  described below. Child-process egress remains governed by the code/process
  boundary, not by destination-safe application egress.

Precise per-argument provenance is not achievable through an LLM: the model
reads trusted and untrusted content together and emits new strings. For the
remaining audience-bearing sinks, conservatively use the turn's integrity
state: a model-composed body/args is allowed only before any untrusted
active-ingest source, otherwise blocked or one-use declassified. This does not
apply to exact/session-approved `fetch_url` destinations. It also does not apply
to `web_search` under the explicit fixed-service, single-operator decision above;
that exception accepts the query channel rather than claiming it does not exist.

#### Operator-owned MCP trust posture

Configuring an MCP server authorizes Mimir to connect to that server. It does
not implicitly widen every tool the server advertises. Each `tool_policies`
entry has two independent IFC grants:

- `result_integrity: trusted | untrusted` controls successful result ingestion.
  `trusted` enters IFC as trusted content; `untrusted` enters as untrusted
  `active_ingest`.
- `argument_egress: allowed | taint_gated` controls model-composed arguments to
  that exact tool. `allowed` keeps the tool callable after untrusted active
  ingest, including operator-approved search/read query channels.
  `taint_gated` preserves the external-MCP sink gate.

For example:

```json
{
  "name": "catalog",
  "command": "catalog-mcp",
  "server_config_id": "catalog-production",
  "policy_version": "policy-v3",
  "tool_policies": [{
    "tool_name": "search",
    "classification": "open",
    "adapter_name": "catalog-policy",
    "adapter_version": "adapter-v2",
    "approval_version": "approval-v7",
    "policy_version": "policy-v3",
    "config_digest": "<digest of this immutable server configuration>",
    "schema_digest": "<digest of the approved search input schema>",
    "result_integrity": "untrusted",
    "argument_egress": "allowed"
  }]
}
```

These values are operator grants and may deliberately trade isolation for
capability. They are resolved during discovery and carried through the
server-authored authorization decision; result classification and sink
enforcement do not look them up again by mutable/display name. Widening remains
bound to the immutable `server_config_id` and derived tool identity, canonical
config digest, input-schema digest, and policy version. A new, renamed,
undeclared, schema/config-drifted, tombstoned, or invalid tool cannot inherit a
grant from another tool. Omitted posture fields use the bootstrap defaults
`result_integrity=untrusted` and `argument_egress=taint_gated`; an invalid tool
policy entry is ignored without disabling valid sibling entries on the same
configured server.

The taint continues to gate *code/shell/action* sinks in all cases. By trigger:

- **GitHub / research pollers:** no `fetch_url` capability at all (they fetch via
  their own subprocess; the capability is simply not in their set).
- **Heartbeat:** `fetch_url` allowed against an **operator-approved allowlist**.
  An exact URL is both reachability authorization and the operator's trust signal
  for the response bytes. The framework-written cache sidecar binds a subsequent
  file read to that URL; content without this evidence stays untrusted active
  ingest. The heartbeat fetches exact approved URLs freely; scope-only URLs require
  a clean turn.
  - **Only exact URLs / fixed templates are taint-independent.** A trailing `/*`
    scope grants reachability on clean turns, but falls through to the turn-taint
    gate after untrusted active ingest. Otherwise fetched content could steer the
    agent to a new data-carrying path or query on an approved host
    (`https://approved/leak/<secret>` or `https://approved/?leak=<secret>`) and
    exfil via that host's logs/reflection — "approved to fetch from" != "safe to
    send arbitrary data to."
- **User / operator turns:** **ask-on-first-use per exact URL** — the agent asks
  the first time it wants a destination, the operator approves it (adding that
  **exact URL** to the session allowlist), then it's remembered for that scope. A
  later different path/query on the same host is a fresh ask. Session approvals
  remain exact; operator-configured `/*` scopes are standing reachability grants
  only while the turn carries no untrusted active ingest.

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
review" — routes through **`worklink_run`** (isolated checkout, `observe_evidence`
diff/test validation, reviewed-PR-only durable output). Generic `spawn_*` is
**not** used for poller code work, because it is not contained (§2/§3).

A known contributor's PR is trusted, so its code work runs; an unknown author's
issue/PR, or a non-contributor comment, is untrusted → **notify-only** (§4). So
even the trusted path is contained by Worklink, and the untrusted path does not
autonomously touch code at all.

**Defense-in-depth now (implemented):** worklink passes the pinned OpenCode
runtime a final `OPENCODE_PERMISSION` override containing
`external_directory: {"/**": "deny"}` and `bash: {"*": "deny", ...allowlist}`.
The allowlist is operator-configurable at
`backends.opencode.bash_allowlist`; the built-in Python-oriented default permits
the needed `git *` and `uv *` command families. When omitted, the effective
default is derived from operator-owned `defaults.test_command` as `git *` plus
only its approved build runner. Pure command launchers such as `env *`, `bash
*`, and `make *` are deliberately excluded from derivation because they would
bypass the allowlist. An explicit allowlist replaces derivation and is checked
against the test command at configuration load.
OpenCode evaluates the
last matching permission rule, so Worklink emits the catch-all denial first and
the explicit grants after it. It rejects a catch-all allow entry. Denials become
tool errors captured in the headless run/transcript; there is no `ask` rule that
could wedge waiting for an interactive reply.

**Pinned-runtime verification (2026-07-19):** the runtime shipped at that time
was `opencode-ai@1.17.15`. Its config schema accepts `permission.bash` and
`permission.external_directory`; its permission evaluator uses last-match-wins,
and its external-directory guard is invoked by path-taking file tools. The
runtime's schema and source tree contain no `permission.shell.sandbox`,
`shell.sandbox`, or equivalent OS process sandbox. Its `shell` config field only
selects the shell executable, and the bash tool launches with the host user's
filesystem, process, and network authority. There is therefore no additional
OpenCode sandbox to wire in this version.

**Pin bump re-check (2026-07-29, `opencode-ai@1.18.9`):** the pin moved from
1.17.15 to 1.18.9. The load-bearing negative above was re-checked against the
1.18.9 release artifact and still holds: no `permission.shell.sandbox` and no
`shell.sandbox`, and the binary links no OS process-sandbox mechanism (no
`sandbox-exec`, `seccomp`, `bwrap`, `bubblewrap`, `landlock`, `nsjail`, or
`firejail`). The `permission` and `external_directory` configuration surfaces are
still present. The `sandbox` strings that do occur were attributed and are
unrelated to process confinement: AWS service-name maps
(`mturk-requester-sandbox`), interface translations for a `workspace.type.sandbox`
workspace kind, and session *sharing* (`session.unshare`). The conclusion is
unchanged — there is still no OpenCode-provided OS sandbox to wire in.

Scope of that re-check, so it is not mistaken for the full 2026-07-19 pass: the
npm package is a four-file installer stub, so this was a strings inspection of the
distributed platform binary rather than a source-tree read. Three 1.17.15-era
claims were therefore **not** re-established at the original depth: that the
permission evaluator uses last-match-wins, that the external-directory guard is
invoked by path-taking file tools, and that the `shell` config field only selects
the shell executable. Those are unchanged Worklink assumptions carried forward on
the strength of the earlier pass; re-verify them at source depth before treating
this section as current evidence for an enablement decision.

These controls are **defense-in-depth for trusted code work only**, not an OS
sandbox or a boundary for hostile payloads. File-tool path checks do not revoke
the authority of an allowed command; command arguments can still read or write
outside the checkout and reach the network. That residual is accepted only
because Worklink is trusted-code-only. Untrusted code work remains notify-only
and requires the future isolated-compute substrate before it may execute.

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

- The trust model in one line: *content that is both untrusted and actively
  ingested this turn (trigger content plus live tool reads/fetches) gates code
  execution, unbounded/audience egress, and model-emitted egress payloads;
  auto-recall is informational and never gates.*
- Exact/session-approved `fetch_url` destinations and `web_search`'s fixed service
  are destination-safe and remain usable regardless of turn taint. A `fetch_url`
  destination admitted only by a trailing `/*` scope requires a clean turn.
- `webhook`, `http_request`, and external-MCP arguments are turn-taint gated.
  Prefer config/server-derived payloads; if a model-emitted payload is needed,
  send it before ingesting untrusted content this turn. MCP posture is per tool:
  an operator-configured tool may have trusted results and/or allowed arguments;
  otherwise it fails closed with an untrusted result and gated arguments.
- `worklink_run` and other write-capable code execution require a trusted turn;
  generic `spawn_*` is blocked. A block is the gate working as designed —
  **surface it to the operator (or use the one-use declassify), don't retry the
  same call**.

Applies to any turn under enforcement; **heartbeats and pollers benefit most**
(autonomous, no human to ask mid-turn), so their trigger profiles should carry the
guidance. It reduces needless blocks/declassify churn; it does **not** widen what
is allowed.

### 5.7 Operator-directed channel egress

A clean interactive operator request may direct `send_message` or `react` to a
channel other than the triggering channel, and may use sinks classified as
`CROSS_CHANNEL`, `DIRECT_MESSAGE`, or `NOTIFICATION`. This is an authority
allowance, not declassification: `PUBLIC` is excluded, and every source must
still include the operator in its effective ACL.

The allowance is derived only from server-owned ingress state. The turn must be
a bridge-authenticated `user_message`, explicitly `INTERACTIVE`, have no HTTP or
other `event_ingress`, and carry a trusted active-ingest channel source whose
principal, domain, resource, and bridge match the frozen `AuthContext`. The
caller must also have the existing `admin` role required by the channel resource
adapter. Finally, the live monotonic IFC state must evaluate to no untrusted
active ingest; an absent or non-boolean evaluation does not count as clean.
Tainted and non-operator turns therefore remain behind the existing one-use
`approve_declassification` path.

For replies to the triggering channel, a cross-channel `protected_prompt`
source is ignored for the channel-equality conjunct only when its server-created
integrity is `trusted`, which is how framework-authored context is marked.
Untrusted foreign activity still blocks every `SAME_CHANNEL` sink using the
generic gate; there is no `send_message`-specific exception.

---

## 6. Decisions and remaining questions

**Settled in review (operator + mimir):**

- **Capability schema → named capabilities**, validated against the tier table;
  a manifest cannot self-grant or mutate its own authority (§5.1).
- **Content trust → derived from source identity, not turn ownership** (§4).
  This is the integrity model and it supersedes the earlier "operator-trust"
  framing. Anchors: the GitHub relationship graph (collaborator / org member),
  the operator-approved URL list, and internal triggers. mimir's position — that
  integrity is an *enablement prerequisite*, not a multi-user-someday concern —
  is adopted: the confused-deputy case is closed **now**, single-operator
  included.
- **Network egress → §5.4**: the line is **destination reachability**.
  `fetch_url` exact/session approvals and fixed templates (plus per-hop redirect
  re-check) and `web_search`'s fixed provider are taint-independent; scope-only
  fetch destinations require a clean turn. The low-bandwidth fetch invocation-
  pattern channel and search-provider query logs are accepted residuals.
  Audience-bearing `webhook` / `http_request` bodies remain turn-taint gated, and
  external MCP stays fail-closed pending per-server/tool trust posture. User turns
  **ask-on-first-use per exact fetch URL** (no host wildcards). Child-process /
  task-level network confinement is deferred with the isolated-compute substrate
  (§5.5/§6) — nothing untrusted runs in a child process yet.
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
  (self-restrict writes to the checkout). Kernel-support-dependent under Fargate,
  so defense-in-depth, not the primary control.
- **Credential/env minimization:** no `HOME`/provider creds to the child.

**Remaining open:** none — the rev-3 questions are resolved. Trust is wholesale
for trusted sources (§4); the provenance schema + informational recall are set
(§5.3); JIRA trust is by instance (§4). The §7 blockers are closed; current
operator readiness and flip-time checks live in the enablement runbook.

### 6.1 SAGA historical ACL disposition and cutover measurement

`legacy_admin` remains the fail-closed owner and visibility sentinel. Its rank,
meaning, and read grants are unchanged. In particular, it is not redefined to
mean "the operator": doing that would make every future provenance ambiguity
fail open.

**Disposition: mixed migration by persisted evidence.** A historical raw atom
may move from `legacy_admin/legacy_admin` to `<mapped-principal>/private` only
when all of these mechanical conditions hold:

- `created_at` is before the recorded provenance-stamping cutover timestamp;
- `memory_type = raw` and `source_type` is `conversation` or `agent_authored`;
- `origin_channel` exactly matches an operator-reviewed channel-to-principal
  mapping supplied for this migration; and
- the current owner and visibility are both `legacy_admin`.

The rule intentionally leaves service-derived rows, observations and their
evidence intersections, service-owned rows, rows without a mapped channel, and
post-cutover unclassified rows admin/platform-service-only. A channel mapping is
an assertion that the named channel was exclusively the mapped operator's own
channel during the historical period; shared or uncertain channels must not be
listed. Derived rows are not folded into operator-authored rows: their safe ACL
continues to be computed from evidence, and historical derived rows without a
provable common owner stay narrow. This rule is deterministic for a new row and
does not infer an owner from content, source labels, or the current operator.

The migration implementation is `mimir/saga/legacy_acl_migration.py`. It is
dry-run by default and can only apply to a newly created database copy. It
refuses an output equal to the source, refuses an existing output, reports
eligible and changed counts by channel, and is idempotent because changed rows
no longer match the `legacy_admin/legacy_admin` predicate. It must not be run on
the live store as part of enablement measurement:

```bash
# Count only; source is opened read-only.
uv run mimir saga-migrate-legacy-acl \
  --source /snapshot/saga.db \
  --cutover 2026-07-01T00:00:00Z \
  --channel-owner 'discord:operator=user:operator'

# Apply only to a new disposable/reviewable copy.
uv run mimir saga-migrate-legacy-acl \
  --source /snapshot/saga.db \
  --output /snapshot/saga.migrated.db \
  --cutover 2026-07-01T00:00:00Z \
  --channel-owner 'discord:operator=user:operator'
```

**Measurement protocol.** After provenance stamping is fixed, replay a
representative period of normal recall against a live-store snapshot using the
privileged/admin view. Write one JSON object per recall to a JSONL trace. The
required fields are:

```json
{
  "ts": "2026-07-10T10:00:00Z",
  "surface": "automatic_recall",
  "caller_kind": "interactive_user",
  "trigger": "user_message",
  "scope": {"principal": "user:operator"},
  "pathways": {
    "semantic": ["atom-before-rrf", "..."],
    "keyword": ["atom-before-rrf", "..."],
    "triple": []
  },
  "results": ["final-permissive-result-after-production-ranking"]
}
```

`pathways` must contain the privileged candidate lists before ACL filtering and
RRF. `results` must contain the permissive final observation/raw IDs after the
normal activation, confidence, fusion, scoring, and top-k stages. `surface` must
distinguish at least automatic pre-message recall and model-invoked
`memory_query`; `caller_kind` must distinguish interactive users, admins,
ordinary services, and platform services when present. `scope` uses the same
fields as SAGA's `AuthorizationScope`: `principal`, `is_admin`, `is_service`,
`is_platform_service`, `service_canonical`, and `readable_domains`. These labels
are measurement inputs only and never grant runtime authority.

Run the read-only analyzer against the same snapshot:

```bash
uv run mimir saga-enforcement-replay \
  --db /snapshot/saga.db \
  --trace /snapshot/representative-recall.jsonl \
  --cutover 2026-07-01T00:00:00Z \
  > /snapshot/saga-enforcement-report.json
```

The report breaks losses down by surface, caller kind, and trigger. Its
`pre_fusion_*` fields count candidates strict scope removes before RRF (both
pathway occurrences and per-event unique atoms); these losses cannot be inferred
from returned top-k IDs. Its `post_fusion_*` fields count permissive final
results hidden by strict scope. `legacy_admin_corpus` and
`legacy_admin_exclusions` independently split genuinely historical rows from
rows created at or after the stamping cutover.

The sandbox validation uses a synthetic mix of historical legacy, continuing
legacy, private operator, public, and service rows; it is not represented as a
live result. The operator must paste the generated report below before making a
flip decision:

```text
Live representative period: PENDING OPERATOR REPLAY
Cutover timestamp: PENDING
Trace/store snapshot identity: PENDING
Report: PENDING
```

The report is invalid if `legacy_admin_corpus.post_cutover` is non-zero (after
allowing for explicitly imported historical data), because that means the
stamping defect is still producing unclassified rows and the target is moving.
The operator must also inspect `missing_trace_atom_ids`; a non-empty value means
the trace and snapshot do not correspond. Neither the analyzer nor migration
changes `MIMIR_ACCESS_CONTROL_ENFORCED`; enforcement remains a separate operator
decision made only after the live report and migration-copy review.

---

## 7. Other enablement blockers (closed)

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

The remaining chainlinks are also **closed**:

- **#922** — trusted-service autonomous maintenance uses the bounded maintenance
  shell profile.
- **#923** — the suite is enforcement-clean. The standing
  `tests / pytest-enforced` CI job now runs the full suite with enforcement on and
  the shipped model default; see the enablement runbook for the measured results
  and skip-parity guard.

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
5. **Application network-egress boundary** (§5.4): destination allowlist of exact
   URLs plus explicit trailing `/*` scopes. `fetch_url` is taint-independent only
   for exact/session-approved destinations and fixed templates, with **per-hop
   redirect checks**; scope-only destinations require a clean turn. `web_search`
   is taint-independent through its fixed pre-approved service. Audience-bearing
   `webhook` / `http_request` payloads remain integrity-gated; external MCP remains
   fail-closed pending per-tool posture. User fetches **ask-on-first-use per exact
   URL**. Child-process / task-level network confinement is **deferred** with the
   isolated-compute substrate (§5.5/§6) — trusted-only child code today means
   nothing untrusted to confine yet.
6. **opencode file-permission** for worklink (§5.5): set `external_directory` deny
   + `permission.bash` to a **deny-by-default operator-configurable allowlist**
   (not `ask` — headless wedges); verify `shell.sandbox` against our opencode
   version. Defense-in-depth for trusted code work only, not a confinement proof.
6a. **Enforcement-aware prompt guidance** (§5.6): a flag-gated prompt block +
   heartbeat/poller profile guidance describing the trust/taint model — ergonomics
   only, never a boundary.
7. **Enable-time verification**: #922 and #923 are closed. Require the standing
   enforced CI job and the ordinary suite to remain green, then follow the
   operator enablement runbook in [`../authorization.md`](../authorization.md).
   (The other §7 review items — #1140–1144 — are also merged.)
