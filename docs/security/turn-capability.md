# Turn capability: which turns may run several bounded steps

Status: **proposed**.

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

## 4. Proposal

**Make the active-ingest restriction conditional on the requested sink's
boundedness, rather than absolute.**

Today an untrusted active ingest refuses every subsequent shell sink. The proposal
is that it refuse only the **unbounded** ones. After untrusted output has entered
the turn:

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

Under this proposal the carve-out becomes a special case of the general rule and
is expected to be removable. That remains an equivalence this document does not
claim without proof.

### What is not proposed

- No change to what output is labelled `untrusted`. Bounded output is untrusted.
- No change to the `active_ingest` effect for bounded output. It stays an ingest.
- No relaxation of the predicate itself. It keeps refusing unbounded sinks after
  ingestion, which is its whole purpose.
- No change to arbitrary shell before ingestion, which remains admitted.

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

## 8. Reversibility, and the required negative regressions

The change is a condition in sink authorization plus an additional execution path.
Backing it out restores the absolute active-ingest refusal and routes everything
through `bash -lc`. No persisted state, no migration, and no relabelled data to
unwind — which is a consequence of the proposal touching authorization rather than
classification.

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
