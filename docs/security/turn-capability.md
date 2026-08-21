# Turn capability: which turns may run several bounded steps

Status: **proposed**.

A turn that legitimately needs more than one shell step is refused after the
first. The mechanism that prevents this already exists and works, but it is keyed
on an incidental state object rather than on the property it is trying to express,
so exactly one turn kind benefits from it.

This document is scoped to that keying. It depends on the provenance model in
`provenance-model.md` for one invariant, stated in §6, and it carries a security
constraint established in review that any design here must satisfy.

## 1. The defect

`ifc_label_blocked:shell_process` accounts for **1,765** real would-blocks over
eight days on the live deployment — 26.4% of all `would_block: true` shadow
decisions, the second largest population after declared-capability gaps.

By principal:

| principal / trigger | events |
| --- | --- |
| operator `user_message` (no service principal) | 945 |
| `poller:github-activity` | 628 |
| `poller:github-ci-watch` | 178 |
| `synthesis`, `heartbeat` | ~14 |

The shape is the same in each: a turn runs one authorized command, that command's
output enters the turn's label set as an untrusted **active ingest**, and the sink
gate then refuses the next command before any tier exemption can admit it. The
turn gets one step.

## 2. The mechanism, and why one turn kind escapes it

`classify_protected_result` contains a carve-out for `shell_exec` and
`bash_async`, gated on `repo_review_state is not None`. It emits a *complete*
`SourceLabel` with `integrity="untrusted"` and
`integrity_effect="informational"`, and its comment states the reasoning
directly:

> A review turn necessarily needs several shell steps. Preserve the output's
> untrusted integrity without treating each authorized command as a new active
> external ingest that would deadlock the next step.

That is the correct behaviour, and it is the behaviour every turn in §1 needs. But
`repo_review_state is not None` is not the property being described. The property
is *this turn is authorized to run several bounded steps*. The state object
happens to be present on review turns and absent everywhere else, so it works for
the one kind it was written against and silently fails for the rest.

This is the same keying error the provenance work found in a different place, and
the same one that produced the unbound `--repo` operand: policy attached to a
proxy for a property rather than to the property.

## 3. The constraint any design must satisfy

Extending that carve-out to operator turns by relaxing its condition was
attempted, reviewed, and **reverted** in PR #1617. The review finding is the
central design input here, and it is correct:

The review-turn carve-out is safe **because repo-review shell is constrained to a
server-owned argv profile.** An operator `shell_exec` is arbitrary `bash -lc`.
Classifying every successful result from an unrestricted shell as
`untrusted/informational` disables precisely the `untrusted + active_ingest`
predicate the sink gate depends on. Output that a first command drew from a
malicious repository or a network response could then steer an arbitrary second
command in the same turn, with no gate intervening. Retaining
`integrity="untrusted"` does not preserve that protection once the effect is
informational.

So the safety of the existing carve-out does not come from the turn *kind*. It
comes from the commands being **bounded**. Any design that keys on turn kind
inherits the wrong justification.

## 4. Proposal

**Gate the informational classification on the producing command being bounded,
not on the turn.**

When a shell result is classified, ask whether the command that produced it was
admitted by a server-owned argv profile. If it was, emit the complete
`untrusted`/`informational` entry the review-turn carve-out emits today. If it was
not, leave it as an active ingest, and that turn gets one unrestricted step —
which is the correct outcome, not a limitation.

The machinery exists. `_target_matches_read_only_shell_command` validates argv for
`pwd`, `ls`, `wc`, `grep`, `jq`, `rg`, and `git` read subcommands, plus the
Chainlink executable allowlist. The repo-review matchers validate `gh issue view`
and `gh pr view`, now with `--repo` operands bound to configured repositories.
Between them they cover the observed operator command set:

    56  gh pr view          43  git log --all       41  git grep -n
    37  gh issue view       31  chainlink issue show 29  git status --short
    25  gh run view

with one exception: **31 `python - <<PY` invocations are not bounded and must
stay one-per-turn.** That is arbitrary code execution; it is the case the
constraint in §3 exists for, and admitting it would be the reverted change in
another costume.

### What this is not

- Not a relaxation of the `untrusted + active_ingest` predicate. That predicate
  keeps doing exactly what it does; this changes which results acquire the
  `active_ingest` effect.
- Not a change to the sink gate, `_get_allowed_sinks`, or
  `_source_is_triggering_channel_compatible`.
- Not a change to the existing `repo_review_state` branch, which should be
  reachable through the same bounded-command test once it exists, and then
  removed as redundant rather than left as a parallel path.

## 5. The poller population, resolved

An earlier revision of this document left the 806 poller events unexplained and
made establishing them task one. They are now measured, and they split on whether
the turn carried a pull-request scope at all:

| principal | `scope_provenance` | `scope_id` | events |
| --- | --- | --- | --- |
| `poller:github-activity` | `None` | absent | 544 |
| `poller:github-ci-watch` | `None` | absent | 178 |
| `poller:github-activity` | `poller_payload` | present | 90 |

**722 of 806 carry no scope.** They are not review turns, never acquire
`repo_review_state`, and the existing carve-out therefore does not and should not
reach them. That is §2 applying directly rather than an anomaly: these turns run
several bounded steps and are refused after the first, exactly like the operator
population. The proposal in §4 covers them.

**90 carry a scope and are still refused.** Those are the genuine residue. A turn
holding a `poller_payload` scope should have `repo_review_state` and so should
already receive the informational classification for a shell result.

Two candidate causes, and this document does not choose between them because the
distinction is not measurable from the shadow decision alone:

- The tainting source was **not a shell result.** A review turn that reads a file
  acquires an active-ingest entry from the read, and the shell carve-out is
  irrelevant to it. If so, those 90 belong to the provenance work rather than
  here, and this design will not close them.
- The preceding shell result **failed.** The carve-out is additionally gated on
  `not failed`, so a command that errored contributes an active ingest even on a
  review turn.

Either way the number is small and attributable elsewhere. **Scope of the
proposal, stated precisely: 945 operator events plus 722 unscoped poller events =
1,667 of the 1,765.** The remaining 98 (90 scoped poller plus ~8 synthesis and
heartbeat) are out of scope here and should be re-measured after the provenance
change lands, since the first candidate above predicts most of them dissolve.

## 6. Dependency on the provenance model

This design requires that **"came from a bounded introduction" stay expressible
separately from "is untrusted."** `provenance-model.md` keeps those on independent
axes — trust in `integrity`, exposure kind in `integrity_effect` — and a bounded
command's output is `untrusted`/`informational` precisely because both axes are
available. Collapsing them, in either direction, forecloses this proposal.

Nothing here requires the provenance change to land first. The two are
independent in implementation and coupled only through that invariant.

## 7. Reversibility

The change is a condition at one classification site. Backing it out restores the
`repo_review_state` gate; no persisted state and no migration are involved.

The signal that it is wrong is a sink permitted after an *unbounded* command's
output entered the turn. That is directly testable and should be the required
negative regression: a turn whose first command is outside every profile must not
reach a shell sink on its second.
