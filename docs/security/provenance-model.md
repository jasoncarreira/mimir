# The turn resource ledger

Status: **proposed**. This document argues that the information-flow model Mimir
already specifies is correct, that the implementation departs from it in two
specific ways, and that closing those departures is the right fix. It makes a
recommendation rather than presenting options, and states what would show the
recommendation to be wrong.

Scope is the ledger — what a turn records about the resources it has accessed,
and who decides the consequence. Turn capability is **out of scope** and appears
only in §10, as the downstream consumer whose constraint this design must not
foreclose.

## 1. The model, as already specified

`InformationFlowLabels` is a per-turn, monotonic, append-only record. Its
`sources` field is a tuple of `SourceLabel`, one per resource the turn has
touched, and each entry carries the resource's identity *and* its nature:

- identity — `principal`, `domain`, `resource_id`, `bridge_instance`
- reach — `authorized_principals`, `sensitivity`
- nature — `source_kind`, `integrity`, `integrity_effect`

Its docstring enumerates the intended contributors: inbound and folded messages,
recent history, automatic memory/session/skill/file injection, attachments,
continuation context, and protected tool results. Labels are monotonic and
unknown labels fail closed.

That is a resource ledger. Nothing in this document proposes a new model; the
argument is that the implementation does not honour this one.

## 2. Two departures

### 2.1 Entries are degraded at write time

`classify_protected_result` produces a well-formed entry for a filesystem result
only on the trusted branch:

```python
source = protected_result_source(
    auth_context, principal="filesystem", domain="filesystem",
    resource_id=..., bridge_instance="filesystem",
)
if source.integrity == "trusted":
    return InformationFlowLabels().with_source(source)
return _incomplete_protected_result(domain, args)
```

Every other case falls to `_incomplete_protected_result`, which constructs
`principal=None`, `bridge_instance=None`, `authorized_principals=frozenset()`,
`integrity="untrusted"`. The `resource_id` survives; the identity does not, so
`is_complete` is false.

The ledger entry therefore records *"something untrusted happened"* rather than
*"resource X is untrusted."* This is not a case of the server lacking the
information. For a filesystem read the server knows the principal, the domain,
the path, and the bridge instance — the call sites above pass them explicitly.
The information is discarded, and then the turn fails closed because it was
discarded.

The distinction being lost is the one the ledger exists to make: **an
unattributable source and an attributed untrusted source are different risk
classes, and they currently share one representation.** An unattributable source
should fail closed everywhere. An attributed untrusted source is what an
information-flow system is *for*.

### 2.2 Policy is decided in the write path

`classify_protected_result` decides both what to record and, through the
trusted-versus-incomplete branch, what the consequence will be. Recording and
policy are the same step.

That coupling is the structural cause of a recurring defect class. Because policy
is decided when a result is recorded, every new tool and every new turn kind must
have its policy re-decided there. The visible symptoms are the per-tool domain
tables (`_PROTECTED_RESULT_DOMAINS`, `_OPERATION_READABLE_DOMAIN`) and
hand-written carve-outs — most tellingly the `repo_review_state` branch, which
exists specifically to emit a *complete* entry with `integrity="untrusted"` so
that a review turn's successive shell steps do not deadlock each other. That
carve-out is this document's recommendation, implemented once as an exception for
the one turn kind that could not tolerate the general path.

## 3. Consequences

A degraded entry cannot satisfy any sink check.
`_source_is_triggering_channel_compatible` rejects on its first line:

```python
if not getattr(source, "is_complete", False):
    return False
```

Three consequences follow, all observed:

1. **The per-kind rules are unreachable.** The predicate's final line is
   `return source_kind in {"protected_tool", "auto_recall", "mcp"}`, which a
   filesystem read would satisfy. It never runs.
2. **`has_untrusted_active_ingest` is not involved.** Two independent
   investigations eliminated every branch gated on that predicate and still
   observed integrity-dependent behaviour, because the gating is completeness.
3. **The turn loses the ability to reply.** The final `SAME_CHANNEL` branch of
   `_get_allowed_sinks` requires *every* entry to be compatible, so one degraded
   entry suppresses the reply to the operator who asked.

Measured over eight days of `shadow_tool_decision` events on the live
deployment: `ifc_label_blocked:file` and `ifc_label_blocked:network` account for
**661 real would-blocks**. Instrumented on one session, same tool, three paths,
differing only in the read:

| read | `bridge_instance` | ACL | `is_complete` | outcome |
| --- | --- | --- | --- | --- |
| `docs/trust-probe.md` | `filesystem` | populated | true | reply delivered |
| `memory/core/00-identity.md` | `filesystem` | populated | true | reply delivered |
| `saga.toml` | `None` | empty | **false** | `ifc_label_blocked:same_channel` |

Both admitted paths and the refused one are inside the agent's own home. The
difference is not a boundary anyone drew; it is which paths are in the admitted
read set.

A separate and larger group — `read_scope` and `write_scope`, 1,482 events — is
**not** caused by this mechanism. Those are path-prefix admission decisions at a
different layer. They share the shape of keying policy on a proxy for provenance,
but they are a distinct design and are out of scope here.

## 4. The gate is not the defect

`_source_is_triggering_channel_compatible` is behaving correctly. It refuses to
reason about a structurally invalid entry, which is the right default: an entry
without a principal, bridge instance, or ACL cannot be compared against a
destination on any axis.

It follows that relaxing or narrowing that predicate is the wrong repair. It
would compensate for malformed input by weakening a correct check, and it would
weaken it for every source kind rather than for the one that is malformed. This
rules out the fix currently proposed for issue #1592.

## 5. Recommendation

**Separate recording from policy, and never degrade a ledger entry the server can
attest.**

### 5.1 Recording becomes mechanical

When a protected result's provenance is known to the server, record a complete
entry: real `principal`, `domain`, `resource_id`, `bridge_instance`, and ACL,
with `integrity` set to what the server determined. No branch on trust decides
whether to record faithfully.

`_incomplete_protected_result` is retained and narrowed to the case it actually
describes: provenance the server genuinely cannot attest.

### 5.2 The two nature axes stay independent

- `integrity` answers *do we trust this content* — `untrusted` for an external
  or unattested read.
- `integrity_effect` answers *what kind of exposure this was* —
  `active_ingest` for a fresh external ingestion, `informational` for content
  whose introduction the server itself bounded.

An untrusted file read records `integrity="untrusted"` with
`integrity_effect="active_ingest"`. That keeps shell and network gated, because
reading untrusted content and then executing is the confused-deputy risk. The
`informational` effect is not a synonym for "safer content"; it is reserved for
introductions the server constrained, which is the seam §10 depends on.

### 5.3 What this changes, concretely

The blanket `source_kind in {"protected_tool", "auto_recall", "mcp"}` rule
becomes reachable and therefore load-bearing. That is the main review burden of
this proposal and should be weighed rather than noted.

It is defensible. That rule governs
`_source_is_triggering_channel_compatible`, which is flow to the **triggering
channel only** — the agent telling the operator what it read, in response to the
operator asking. That is not exfiltration; it is answering the question. The
dangerous sinks are separately gated and unaffected: `SHELL_PROCESS` and `FILE`
on `trusted_operator_turn and not has_untrusted_active_ingest`, cross-channel
through `_is_admin_operator_turn`.

So this proposal does not widen a dangerous sink. It restores the ability to
reply, which is the symptom in §3 that is most clearly wrong.

## 6. What a sink asks of the ledger

Today every sink asks the same question, via `all()` over the entries: is every
entry compatible with this destination? Worst-element-wins.

That is sound and coarse. It is why a single `saga.toml` read costs the turn its
reply even though nothing derived from that read is in the payload.

The precise alternative is per-sink relevance: the worst integrity among entries
that could have *influenced this payload*, rather than among entries the turn
merely accessed. That requires tracking influence rather than access, which is a
substantially harder problem and is not proposed here.

**Recommendation: keep worst-element-wins.** Two reasons. Getting relevance wrong
fails *open* — an entry wrongly judged irrelevant silently permits a flow, where
a wrongly-retained entry only over-refuses. And the measured 661 events are
caused by degraded entries, not by the conjunction; complete entries let the
per-kind rules discriminate, which recovers most of the precision that relevance
would buy.

Recorded as a known limitation, not as settled: a long autonomous turn that reads
many resources accumulates a monotonic ledger it cannot shed except by audited
declassification, so its available sinks narrow as it runs.

## 7. The rejected alternative

Ratify degraded entries as the taint representation and document the current
behaviour as intentional.

Cheapest, and it preserves a property that is easy to state: an untrusted read
costs the turn every sink, unconditionally. It is rejected because:

- It discards information the server holds, then fails closed because it was
  discarded.
- It leaves the `integrity` axis inapplicable to the largest source class, since
  completeness rejects before integrity is consulted.
- It keeps "unknown" and "untrusted" indistinguishable to every gate.
- It is already being worked around in-tree by the `repo_review_state` carve-out,
  and every future turn kind that cannot tolerate the general path will need the
  same exception.

## 8. Consumer audit

Nothing depends on entries being degraded. Every consumer of `is_complete` was
enumerated, because a change that makes entries complete would break anything
using incompleteness as a cheap "distrust this turn" signal.

| site | use | effect of complete entries |
| --- | --- | --- |
| `access_control.py` `_source_is_triggering_channel_compatible` | rejects incomplete entries first | the subject of this document |
| `access_control.py` cross-channel admin-operator branch | requires `all(is_complete)` **to admit** | see below |
| `agent.py` protected prompt loader | raises `ValueError` unless every entry is complete | strengthened; it already demands what this provides |
| `models.py` `SourceLabel.derived` | intersects ACLs only when all inputs are complete | strengthened; degraded inputs currently yield an empty ACL, which then fails completeness downstream - a cascade this removes |
| `models.py` `is_complete` | the definition | unchanged |

The second row is the one to check, because it is the only place where
completeness *permits* rather than requires. That branch is additionally gated on
`_is_admin_operator_turn`, which requires `has_untrusted_active_ingest is False`.
Because §5.2 keeps an untrusted read at `integrity_effect="active_ingest"`, a
turn that has read untrusted content cannot satisfy it, so the branch stays
closed. The protection is carried by the effect axis rather than by
degradation — which is the separation §5.2 exists to establish.

## 9. Reversibility

This is a change at one construction site. Backing it out means restoring the
degraded entry shape; completeness rejection resumes immediately, with no
migration and no persisted state to unwind.

The signal that it is wrong is a **new allow** appearing in the shadow decision
log for a sink category other than the triggering channel. That is detectable
today: every decision is recorded with `would_block`, and the sink categories are
separately tested. Enforcement remains off, so the first evidence arrives from
the shadow log rather than from a live failure.

## 10. Downstream consumer, out of scope

Turn capability — whether a turn may run several shell steps — is a separate
design, named here only to record the constraint it places on this one.

The `repo_review_state` carve-out is safe **because repo-review shell is bounded
by a server-owned argv profile.** An operator `shell_exec` is arbitrary
`bash -lc`. Extending the same classification to it without that precondition
would disable the `untrusted + active_ingest` predicate that stops first-command
output drawn from a hostile source steering a second arbitrary command in the
same turn. That widening was written, reviewed, and reverted; the safe form gates
on the *producing command* being bounded rather than on the turn kind.

The constraint: **"this result came from a bounded introduction" must remain
expressible separately from "this result is untrusted."** §5.2 satisfies it by
keeping the two axes independent — bounded-ness in `integrity_effect`,
trust in `integrity`. Collapsing them would foreclose the safe form of the
turn-capability fix.

## 11. Blocked consumer

**ACP: a tainted turn cannot answer the client at all** (issue #1592). Its stated
fix — narrowing the exact-origin `SAME_CHANNEL` reply path — is ruled out by §4,
and its symptom is §3's third consequence. It needs this decision first.

The Mimir Hands client-provider work was investigated as a possible consumer and
**ruled out by mechanism**: no `SourceLabel` is constructed anywhere in the Hands
admission path, and `_auth_context_for` never consults the provider declaration.
Its remaining refusal is a scope or target-resolution question, unrelated to this
document.
