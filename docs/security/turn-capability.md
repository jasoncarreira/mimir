# Turn capability: which turns may run several bounded steps

Status: **accepted for Arm 2 implementation**.

Sections 1-3 retain the history that ruled out producer-side relabelling and
command-text inference. Sections 4-8 are the design of record for the selected
Arm 2 implementation. Earlier coverage questions and the possible removal of an
existing carve-out are resolved below; they are no longer open design choices.

A turn that has ingested untrusted content cannot run a further shell command. The
mechanism that prevents this already exists and works, but it is keyed on an
incidental state object rather than on the property it expresses, so exactly one
turn kind benefits.

An earlier revision of this document proposed recognising a bounded command by
**matching its text at classification time**. Review established that this is
unsound, and it was the document's central mechanism. §3 records why, and §4
replaces it. The correction matters because the unsound version would have
recreated the exact security failure the document argues against.

## 1. The defect

`ifc_label_blocked:shell_process` accounts for **1,765** real would-blocks over
eight days — 26.4% of all `would_block: true` shadow decisions.

| principal / trigger | events | carries a PR scope |
| --- | --- | --- |
| operator `user_message` | 945 | n/a |
| `poller:github-activity` | 634 | 90 of them |
| `poller:github-ci-watch` | 178 | none |
| `synthesis`, `heartbeat` | ~8 | — |

The shape is identical in each: a turn runs one authorized command, that
command's output enters the label set as an untrusted **active ingest**, and the
sink gate refuses the next command before any tier exemption applies.

This is a **state transition, not a quota.** The first command is admitted because
the turn has not yet ingested anything untrusted; it is that command's own output,
entering the label set as an active ingest, that closes the door behind it. The
boundary is *before versus after untrusted ingestion*, and describing it as "one
command per turn" misstates it - a single invocation may chain, pipe, and
substitute freely, so a count would bound nothing.

## 2. Why one turn kind escapes it

`classify_protected_result` carries a carve-out for `shell_exec` and `bash_async`
gated on `repo_review_state is not None`, emitting a complete `SourceLabel` with
`integrity="untrusted"` and `integrity_effect="informational"`. Its comment states
the reasoning:

> A review turn necessarily needs several shell steps. Preserve the output's
> untrusted integrity without treating each authorized command as a new active
> external ingest that would deadlock the next step.

That is the behaviour every population in §1 needs. But `repo_review_state is not
None` is not the property being described. The property is *this command was a
bounded introduction*, and the state object is a proxy that happens to correlate
with it on review turns.

## 3. Why the carve-out is safe there, and why text matching is not

Extending the carve-out to operator turns by relaxing its condition was attempted
and **reverted in #1617**. The finding: the carve-out is safe because repo-review
shell is constrained to a server-owned argv profile, while an operator
`shell_exec` is arbitrary. Marking unrestricted shell output informational
disables the `untrusted + active_ingest` predicate that stops first-command output
drawn from a hostile source steering a second arbitrary command.

So safety comes from the command being **bounded**, not from the turn kind.

The first revision of this document then proposed asking, at classification time,
whether the producing command *matched* a server-owned profile. That is unsound,
for a reason specific to how operator shell executes:

```python
["bash", "-lc", login_shell_command(command)]
```

An operator or admin `shell_exec` runs through a **real login shell**. It sources
profile files and honours aliases, functions, pipes, chaining, and substitution.
So text reading `git status --short` does not establish that only `git status`
ran — a shell function named `git` could do anything. Matching text after the fact
would classify an unrestricted execution as bounded, which is precisely the
failure this section rules out.

Classification also receives the **original** request, while service binding
rewrites only the *execution* request — so a matcher replay at classification time
is not even looking at what ran.

The conclusion is not that operator turns cannot have bounded steps. It is that
boundedness must be a **fact produced by the execution path**, not a property
inferred from a string.

### And a bound execution does not make its output safe

A second revision of this document then claimed that a bound argv executed with
`shell=False` "introduces no content the model can be steered by." That is also
false, and it is the more dangerous of the two errors.

`shell=False` constrains **what the process executes**. It says nothing about
**what the output contains**. Every command proposed for the bounded path returns
attacker-controlled text by design: anyone can file the issue that `gh issue view`
reads, write the pull-request body that `gh pr view` returns, or land the content
that `git grep` matches.

So marking bounded output `informational` would clear the active-ingest predicate
and thereby admit a subsequent **arbitrary** `bash -lc` — laundering untrusted
content into a state where unrestricted shell is permitted. That is precisely the
outcome §8's regression exists to forbid.

The property that matters is therefore not the trustworthiness of the *producer*.
It is the boundedness of the *consumer*.

## 4. Selected Arm 2 design

**Make the active-ingest restriction conditional on the requested sink's
server-authored boundedness, rather than absolute.**

Before Arm 2, an untrusted active ingest refuses every subsequent shell sink. The
selected rule is that it refuse only the **unbounded** ones. After untrusted output
has entered the turn:

- a further **server-bound** command remains admissible, because ingested content
  cannot steer a command whose argv the server authored;
- an **unbounded** `bash -lc` remains refused, exactly as today.

This puts the mechanism in **sink authorization**, not in result classification.
Classification keeps doing what it does — a bounded execution's output is
`untrusted`, and it stays an active ingest. Nothing is relabelled as safe. What
changes is which sinks that state forbids.

Authorization is the right place because it already runs *before* execution and
already parses the requested command to decide admission, so it can determine the
requested sink's boundedness without inference. And the binding fact from §3 is
what makes the requested command's boundedness a server-authored property rather
than a guess about text.

### Why this finally explains the existing carve-out

The `repo_review_state` branch is safe, and the reason is not that review-turn
output is trustworthy — it is not, for the same reasons as above. It is safe
because **every** command on a review turn is argv-bounded. Ingested content has
no arbitrary sink to steer into, so the door never needs closing.

An operator turn has both paths available. That asymmetry, not the turn kind, is
why copying the classification there is unsafe — and why the fix belongs at the
sink rather than at the label.

The existing service/Chainlink carve-out is retained. The selected profile admits
both Chainlink query and mutation forms, while the service branch provides the
narrower query/mutation distinction. Removing it would widen service behavior and
is not required to implement Arm 2.

### Selected profile and conditional coverage

Arm 2 uses the existing `scheduler_read_only` profile. It does not use
`repo_review`: repository-sensitive commands in that profile depend on immutable
`RepoReviewState`, authority an ordinary operator turn does not have. No profile
allowlist changes as part of this design.

The historical sample has a syntax ceiling of approximately **60/293** commands:
31 `chainlink issue show ...` queries and 29 `git status --short` calls. This is a
ceiling, not guaranteed runtime coverage. Chainlink binding also requires tracker
cwd resolution. Git binding requires an authorized configured Git root and a
successful hardening pass. The sample contains no cwd or deployment-configuration
evidence, so actual coverage may be lower.

Profile matching is necessary but not sufficient. Arm 2 disables the parser's
configured-project-test branch and applies family-specific confinement before it
issues a binding. `jq` and `rg -L` remain unbound because their filter/module or
symlink-following surfaces are not proven filesystem-bounded. These exclusions,
and the Git intersection below, narrow the selected profile without changing its
allowlist for existing callers.

### Preparation outcomes

Every qualifying operator `shell_exec` request has one exhaustive preparation
outcome:

- **`BOUND`**: parsing, executable pinning, cwd resolution, family confinement,
  and hardening succeeded. Authorization and execution consume the same immutable,
  request-scoped exact-argv binding.
- **`SOFT_UNBOUND`**: the command may retain the existing pre-ingest `bash -lc`
  behavior, but only if an execution-time live provenance read is exactly
  untainted. Profile misses, shell syntax, configured project tests, `jq`,
  `rg -L`, and scheduler-admitted Git forms outside the hardener intersection are
  soft-unbound.
- **`HARD_REFUSED`**: no process starts in shadow or enforced mode. Invalid
  command shape, executable-pin failure, cwd or root violation, reader confinement
  failure, hardener infrastructure failure, and binding mismatch or reuse are hard
  refusals.

A bound nonmutation or query may execute exact argv whether live ingestion is
false, true, or indeterminate, subject to unrelated existing gates. Chainlink
mutations have the stricter always-on rule below.

Only `SOFT_UNBOUND` can fall back to the login shell. Immediately before that
fallback, execution rechecks live IFC state. `true`, missing, exceptional,
non-boolean, or otherwise indeterminate state fails closed; only exact `False`
permits `bash -lc`. This closes a taint arrival between authorization and
execution and applies in both shadow and enforced modes.

### Binding, cwd, readers, and Git

The binding is issued by server code after model-supplied internal carriers have
been stripped. It seals the selected profile, exact final argv, tool and call,
request and authorization identities, requested and resolved cwd, and Chainlink
mutation classification. It cannot be selected, forged, copied across a request,
or reconstructed from command text. Any mismatch is a hard refusal, and cleanup
removes the request-scoped carrier on every completion path.

Omitted cwd resolves the process cwd, never sticky interactive `cd`. Explicit cwd
must be absolute, free of NUL and lexical traversal, resolve to a directory, and
remain under the applicable configured root after symlink resolution. Git is
confined to configured maintenance Git roots; readers are confined to configured
non-admin read roots. Implicit `/tmp` is not a Git root.

Reader operands are parsed once, checked as a complete set, and rewritten to
canonical absolute paths. Root escape, unsafe symlinks, missing or unreadable
targets, protected or credential-bearing content, mixed unsafe operands, and
recursive preflight limits hard-refuse the call. Recursive `grep` and `rg` retain
bounded entry, byte, hidden-file, and protected-content checks.

Git binding is the intersection of unchanged `scheduler_read_only` matching and
the existing maintenance Git hardener. The hardener pins cwd and neutralizes
hooks, fsmonitor, external diff, textconv, filters, protocols, credentials, pager,
and optional locks. Hardened non-verbose `status` is bound. Hardened `diff`, `log`,
and `show` are bound only without a literal `--`; verbose status and those forms
with `--` are soft-unbound.

### Always-on Chainlink and audit boundaries

A bound Chainlink query may execute after active ingestion. A bound Chainlink
mutation is stopped when live ingestion is active or indeterminate, regardless of
shadow or enforced mode. The middleware enforces this immediately before process
execution as well as preserving the existing authorization decision and
`chainlink_mutation_blocked_by_untrusted_ingest` refusal. Profile admission never
overrides the mutation policy.

Arm 2 audit records use a fixed, value-free summary containing only
`shell_profile`, `preparation_outcome`, `command_family`, and `binding_rule`.
Tool-call and tool-error records omit command and cwd; hard-boundary records use a
null target and fixed reason; shadow decisions use the fixed `shell_process`
target. No command, cwd, argv, operand, traversed child, credential, or
model-derived refusal text is emitted. Non-Arm-2 audit shapes are unchanged.

### Unchanged constraints

- No change to what output is labelled `untrusted`. Bounded output is untrusted.
- No change to the `active_ingest` effect for bounded output. It stays an ingest.
- No relaxation of the predicate itself. It keeps refusing unbounded sinks after
  ingestion, which is its whole purpose.
- No general change to arbitrary shell before ingestion: soft-unbound commands
  retain that behavior, while the hard failures above start no process.

## 5. Historical sample and the selected ceiling

The earlier revision claimed the observed operator commands were covered by
existing profiles. Run through the real matchers, that is false:

| command | events | `read_only` | `repo_review` |
| --- | --- | --- | --- |
| `gh pr view …` | 56 | no | **yes** |
| `git log --all` | 43 | no | **no** |
| `git grep -n …` | 41 | no | **yes** |
| `gh issue view …` | 37 | no | **yes** |
| `python - <<PY` | 31 | no | **no** |
| `chainlink issue show …` | 31 | **yes** | **yes** |
| `git status --short` | 29 | **yes** | **yes** |
| `gh run view …` | 25 | no | **no** |

So of roughly 293 sampled operator commands, about **194 are admissible by an
existing profile and about 99 are not**. The 1,667/1,765 coverage figure in the
earlier revision was derived from the allowlist's shape rather than from running
the commands through it, and is withdrawn.

That comparison used either existing profile and therefore did not answer which
profile an ordinary operator could safely use. The selected implementation uses
only `scheduler_read_only`, reducing the sample's syntax ceiling to the 60/293
commands recorded in §4. `git log --all`, `gh run view`, and other possible
allowlist additions are not part of this decision. `python - <<PY` remains
arbitrary code on the unbounded path, which is the case §3 exists for.

## 6. Scope

The selected implementation serves existing trusted, non-service operator
`user_message` turns invoking `shell_exec`. It does not grant operator authority
to `bash_async` or extend authority to another principal, channel, tool, or
operation. Admission requires both existing operator authority and the genuine
`scheduler_read_only` binding described in §4.

Explicitly unchanged: every profile allowlist; Arm 1 and service attribution;
unscoped poller authority; trust derivation; the provenance model; and repository
review authorization and result classification. The existing service/Arm 1
Chainlink query/mutation branch remains in place because deleting it would admit
mutations that it currently refuses. The `repo_review_state` informational branch
also remains in place; Arm 2 operator results do not use it.

## 7. Dependency on the provenance model

The dependency is the **opposite** of what an earlier revision of this document
claimed, and getting it backwards is what made that revision unsafe.

This design requires that **untrusted active-ingest provenance survive a bounded
read.** A bounded command's output must continue to be recorded as `untrusted`
with an `active_ingest` effect. The sink gate can only narrow what an ingest
forbids if the ingest is still recorded; a provenance layer that relabelled
bounded output as `informational` would clear the state this design reasons
about, and a later arbitrary `bash -lc` would be admitted. That is the hole §3
and §4 exist to close.

So what must stay independently expressible is not a second *label* on the result.
It is the server-authored **sink binding**, available to authorization when it
judges a prospective consumer. Provenance describes what the turn has ingested;
authorization decides what may consume it. Keeping those separate is the whole
mechanism.

**On the existing carve-out:** the `repo_review_state` branch in
`classify_protected_result` does emit `integrity_effect="informational"` today.
That is **current behaviour, not an invariant of the Arm 2 rule** — and it is the
producer-side relabelling this document argues against. It is safe in place only
because every command on a review turn is bounded, so nothing unbounded remains to
be admitted by the cleared state. The selected implementation leaves that branch
unchanged. Removing or relabelling it requires a separate review and is not a
prerequisite for Arm 2.

Arm 2 ordinary shell results stay on generic classification: `untrusted` with
`integrity_effect="active_ingest"`. Repeated bounded reads monotonically retain
that state. Authorization narrows only the prospective shell sink; it does not
clear or reinterpret provenance.

## 8. Reversibility, and the required negative regressions

The change is a condition in sink authorization plus an additional execution path.
Backing it out restores the absolute active-ingest refusal and routes soft-unbound
pre-ingest commands through `bash -lc`. No persisted state, no migration, and no
relabelled data need unwinding because the design touches authorization and
request-scoped execution rather than classification.

Three properties must be pinned, and the second and third are new to this
revision:

1. **An unbounded sink after ingestion stays refused.** A turn whose first command
   runs through `bash -lc` must not reach an unbounded shell sink on its second,
   even if that command's text would have been profile-admissible had it been
   routed. This fails if anyone reintroduces inference from command text.

2. **The bounded allowance is not reachable by claim.** An unbounded execution must
   not be able to present itself as bounded. The admission must consult the
   server-authored binding fact from §3, so a test should attempt an unbounded
   command whose text matches a profile and assert it is still refused after
   ingestion.

3. **The allowance is stable under iteration.** Because bounded output remains
   `untrusted` with an `active_ingest` effect, a turn that runs many bounded
   commands must still refuse an unbounded one at the end. The state must not decay
   toward permissive as bounded steps accumulate — that would reintroduce the
   laundering path by a longer route.

The third is the property this revision exists to guarantee. The previous revision
would have failed it immediately, because it cleared the ingest state rather than
narrowing what that state forbids.
