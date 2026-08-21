# Turn capability: which turns may run several bounded steps

Status: **proposed**.

A turn that needs more than one shell step is refused after the first. The
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
sink gate refuses the next command before any tier exemption applies. One step per
turn.

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

## 4. Proposal

**Route bounded commands through the existing service-shell binding, and key the
classification on the binding fact it produces.**

Nothing about arbitrary shell changes. `bash -lc` continues to work exactly as
today, one step per turn, output classified as an active ingest. What is added is a
second, narrower path.

The machinery exists and is already the contract for service principals.
`parse_service_shell_argv_with_diagnostics` returns a validated argv, and its own
docstring states the property this design needs:

> The returned argv is both the authorization artifact and the execution
> artifact. Callers must exec it directly with `shell=False`; handing the original
> string to a shell would reintroduce an expansion layer the profile did not
> validate.

So the shape is:

1. A command that a server-owned profile admits is executed as that **bound
   argv**, with `shell=False`. No login shell, no expansion layer.
2. The authorization records that the execution was bound, and to which profile —
   a server-authored fact, not a re-derivable guess.
3. `classify_protected_result` consults that fact. Bound execution earns the
   complete `untrusted`/`informational` entry. Everything else keeps
   `active_ingest`.

This answers the review objection structurally rather than by argument: the fact
exists because the execution genuinely was bound, so there is nothing to replay
and no way for an unbounded execution to be mistaken for a bounded one.

It also makes the `repo_review_state` branch redundant rather than parallel — a
review turn's shell is already bound, so it would earn the same classification
through the same fact. Removing it afterwards is a separate, verifiable step and
is **not** claimed here as equivalent without proof.

## 5. What is actually admissible today

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

That reframes the coverage question usefully. It is no longer "should operators
lose pipes" — arbitrary shell is untouched — but **which commands are worth
admitting to a bounded path.** `git log --all` and `gh run view` are read-only and
plausible additions; each is its own small review with its own argv validation.
`python - <<PY` is arbitrary code and must stay on the unbounded path, which is
the case §3 exists for.

## 6. Scope

The proposal serves any turn whose commands are profile-admissible, which is a
property of the command rather than of the principal. No total is claimed: the
event counts in §1 describe the problem, and how many of them convert depends on
which commands are admitted to the bounded path — a decision this document
deliberately leaves open.

Explicitly out of scope: widening any profile's allowlist, which belongs in
separate reviews; the unscoped poller populations' *authority* to run repository
work at all, which is a different question from whether their steps deadlock; and
the provenance model.

## 7. Dependency on the provenance model

This requires that **"came from a bounded introduction" stay expressible
separately from "is untrusted."** `provenance-model.md` keeps those on independent
axes — trust in `integrity`, exposure kind in `integrity_effect` — and a bound
command's output is `untrusted`/`informational` precisely because both are
available. Collapsing them in either direction forecloses this.

Neither change needs to land first; they are coupled only through that invariant.

## 8. Reversibility, and the required negative regression

The classification change is a condition at one site, and the execution change is
an additional path rather than a replacement — so backing out restores the
`repo_review_state` gate and routes everything through `bash -lc` again. No
persisted state, no migration.

The signal it is wrong is a sink permitted after an **unbounded** command's output
entered the turn. That is the required regression: a turn whose first command runs
through `bash -lc` must not reach a shell sink on its second, even if that
command's text would have been profile-admissible had it been routed.
