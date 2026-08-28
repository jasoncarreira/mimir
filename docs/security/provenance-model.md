# A refused tool call should not contaminate the turn

Status: **proposed**.

An earlier revision of this document diagnosed the defect as untrusted results
carrying degraded provenance, and proposed completing those labels. Review
established that both the diagnosis and the proposal were wrong. This revision
replaces them. The correction is recorded in §7 rather than hidden, because the
wrong version is a documented instance of the reasoning error this design has to
avoid.

## 1. The defect

A tool call that is **refused** contaminates the turn that attempted it. Nothing
is ingested — the call returned no data — and yet the turn permanently loses its
ability to reach any sink, including its ability to reply to the operator who
asked the question.

The turn is not tainted by content. It is tainted by its own refused request.

## 2. The mechanism

Provenance is published by producers, on success only. There are roughly ten
publication sites — `mimir/readonly_backend.py`, `mimir/tools/extra.py`,
`mimir/tools/memory.py`, `mimir/tools/registry.py`, `mimir/tools/shell_async.py`
— all routing through `publish_protected_result`, whose contract is *"publish
exact server-derived sources, including an authoritative empty set."*

The filesystem producer is explicit about the guard:

```python
result = await super().aread(file_path, offset, limit)
if result.error is None:
    self._publish_read_provenance(file_path)
```

with `_publish_read_provenance` documented as *"Publish the path only after
backend resolution and a successful read."* Exact resolved paths, real principal,
real bridge instance.

`classify_protected_result` consumes that published provenance when present. Its
trusted branch is additionally gated on the call having succeeded:

```python
if not failed and authorization.result_integrity == "trusted"
```

So a refused call publishes nothing, cannot take the trusted branch, and falls to
`_incomplete_protected_result`.

The refusal never presents as an exception. `mimir/readonly_backend.py` returns a
result object carrying `error=`, and records the denial separately through
`emit_hard_read_denial`. Because nothing is raised, the `ToolPolicyRefusal` branch
in the classification path is never consulted, and `_result_is_error` reduces the
error result to a generic `failed=True`. That fallback derives its `resource_id` from the
**call's arguments** — `path`, `file_path`, `query`, `turn_id`, `atom_id`,
`job_id` — and sets `principal=None`, `bridge_instance=None`, and an empty ACL,
so `is_complete` is false.

The turn therefore acquires a source describing *what the model asked for*,
attributed to nobody, for a call that returned nothing.

`_source_is_triggering_channel_compatible` rejects it on its first line:

```python
if not getattr(source, "is_complete", False):
    return False
```

and because the final `SAME_CHANNEL` branch of `_get_allowed_sinks` requires
*every* source to be compatible, that single entry suppresses the reply.

## 3. Evidence

Three reads in one session, differing only in path, measured on the live
deployment:

| read | read policy | publisher | entry | reply |
| --- | --- | --- | --- | --- |
| `docs/trust-probe.md` | resolves | fires | complete | delivered |
| `memory/core/00-identity.md` | resolves | fires | complete | delivered |
| `saga.toml` | **refused** (`protected=True`, does not resolve) | does not fire | unattributable | **suppressed** |

`saga.toml` sits at the home root, outside the admitted read subtrees. Verified
directly: `is_protected_read_path` returns true for it and
`resolve_non_admin_read_target` returns none, while all three of `docs`,
`memory/core`, and `state` resolve.

The distinguishing variable is **whether the read succeeded**, not whether its
path was trusted. This experiment has twice been read as trusted-versus-untrusted,
including in issue #1592 and in the previous revision of this document. It is
success-versus-refusal.

`ifc_label_blocked:file` and `ifc_label_blocked:network` account for **661** real
would-blocks over eight days. That population has not been re-attributed between
refused-call contamination and other causes; §6 records that as required work
rather than claiming the whole figure.

## 4. What is *not* wrong

**The fallback is correct.** `_incomplete_protected_result` exists for a call
that succeeded but whose exact returned resources the server cannot enumerate.
Fail-closed is the right behaviour there, and it must be preserved. Completing
that label — the previous revision's proposal — would fabricate provenance: for a
collection tool, a selector or root argument is not the set of resources actually
returned, and for a refused call nothing was returned at all.

**The predicate is correct.** `_source_is_triggering_channel_compatible` refuses
to reason about a structurally invalid entry, which is the right default. An entry
with no principal, no bridge instance, and an empty ACL cannot be compared
against a destination on any axis. Relaxing it would compensate for malformed
input by weakening a correct check, and would weaken it for every source kind.
This rules out the fix currently proposed in issue #1592.

**The publishers are correct.** They publish exact resolved resources on success
and stay silent otherwise. Nothing in this document asks them to change.

## 5. Proposal

**Route a read-policy refusal through `ToolPolicyRefusal`.**

The distinction this needs is not new, and neither is the machinery. `ToolPolicyRefusal`
already exists, and its docstring states the intent exactly:

> A policy refusal happens before a tool exposes protected result data. It is
> returned to the model like other `ToolException` failures, but must not taint
> the turn as though protected content had been ingested.

It is already honoured in the classification path, at both call sites in
`mimir/tools/budget_gate.py`:

```python
except ToolException as exc:
    ...
    if isinstance(exc, ToolPolicyRefusal):
        _record_tool_outcome(tool_name, refused_reason=str(exc))
    else:
        result_labels = _result_labels_for_call(...)
```

A `ToolPolicyRefusal` therefore contributes no source today. The refusal is still
recorded; only the flow label is absent.

Roughly twenty sites already raise it — across `mimir/tools/forge.py`,
`mimir/tools/repo.py`, and `mimir/tools/github_review_guard.py`. The filesystem
read path does not. `mimir/readonly_backend.py` returns a result object carrying
`error=`, and separately calls `emit_hard_read_denial` for the audit record. No
exception is raised, so the `ToolPolicyRefusal` branch is never reached, and the
error result becomes a generic `failed=True`.

So the defect is not a missing policy. It is one path not using an existing,
documented mechanism that its siblings use.

### Why keying on `ToolPolicyRefusal` is safe, and keying on `failed` is not

An earlier revision of this document proposed that *a failed call contributes no
source*. That was unsafe, and the review that caught it was right: `failed` is
derived by `_result_is_error` from `status == "error"` on the tool message, so it
covers every error-status result and handler exception — including an execution
that ran, emitted stdout or an error body the model can read, and then exited
non-zero. Suppressing labels for that class would permit a sink after genuine
ingestion.

`ToolPolicyRefusal` does not have that problem, because it is defined as the
pre-execution case. A command that executed and produced output does not raise it;
that path returns an error result and continues to be labelled exactly as it is
today. The safety property is carried by the type's contract rather than by a
heuristic over result status.

### Required regression

Because the change is scoped to the refusal path, the property to pin is that the
*other* path is untouched: a tool execution that fails after producing
model-visible output must still contribute a source and must still gate egress.
That test should exist regardless of this change, and it is the specific evidence
the review asked for.

### Implementation note

If the backend contract requires returning a result rather than raising —
`aread` returns a result object by design, and the collection operations
accumulate per-path outcomes — then the equivalent is to give the classification
path the same signal the exception carries: *this was a server-authored refusal,
no protected content was exposed*. What matters is that the signal be the typed,
pre-execution one rather than inferred from error status.

## 6. What this does not resolve

- **Attribution of the 661.** How much of that population is refused-call
  contamination versus successful-but-unattributable results is unmeasured.
  Establishing the split is the first task of any implementation, because it
  determines whether this change is the fix or only part of it.
- **`read_scope` and `write_scope`**, 1,482 events, are path-prefix admission at a
  different layer and out of scope here.
- **Turn capability.** Out of scope; see §8.

## 7. Correction to the previous revision

The first revision claimed that provenance is degraded at write time for any
non-trusted read, treated `_incomplete_protected_result` as the general write
path, and proposed completing it. It also analysed a predicate ending
`return source_kind in {"protected_tool", "auto_recall", "mcp"}` and built its
principal review burden on that rule becoming reachable.

Both were errors.

- The publishers in §2 were present and working the whole time. The fallback is
  reached on failure, not on distrust. The drafting error was reasoning backwards
  from an instrumented symptom to a cause that fit, instead of tracing forward
  from the producers.
- The quoted predicate line is from `feature/acp`. On `main`, which this document
  targets, the predicate ends `return source_kind == "protected_tool"`. The line
  was read on one branch and analysed against another.

The second revision then proposed that *a failed call contributes no source*.
That was also wrong, and unsafe in the fail-open direction. `failed` is derived
from `status == "error"` on the tool message, so it covers executions that ran,
exposed model-visible output, and then errored; suppressing labels for that class
would permit a sink after real ingestion. The lesson is the same as the first
error in a different costume — a signal that correlates with the observed case was
adopted as though it were the authoritative one. `ToolPolicyRefusal` is the
authoritative signal, and it was already in the tree, already honoured, and
already documented as meaning exactly this.

The recording-versus-policy observation from the first revision does survive in
reduced form: the trusted-branch decision at `if not failed and
authorization.result_integrity == "trusted"` is a policy choice sitting in the
write path. It is no longer this document's headline, and no change to it is
proposed here.

## 8. Reversibility, and the downstream consumer

The change is a suppression condition at one site. Backing it out restores the
current entry; no persisted state is involved and no migration is required.

The signal that it is wrong would be a flow that becomes permitted because a
label went missing — a call that failed, contributed nothing, and thereby allowed
a sink that a genuine ingestion in the same turn should have gated. That is
testable directly: a turn combining a refused read with a successful untrusted
read must remain gated on the successful one.

Turn capability — whether a turn may run several shell steps — is a separate
design. Its constraint on this one is unchanged: "this result came from a bounded
introduction" must stay expressible separately from "this result is untrusted,"
which is why the two nature axes must remain independent. The unsafe form of that
fix, extending the bounded-command classification to arbitrary shell, was
written, reviewed, and reverted in #1617.

## 9. Blocked consumer

**ACP: a tainted turn cannot answer the client at all** (issue #1592). Its stated
fix is ruled out by §4, and its diagnosis attributes the symptom to taint from an
untrusted path when §3 shows the variable is refusal. Its silent-failure half is
already dispatched separately as chainlink #1325, which is independent of this
decision.
