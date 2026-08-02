# SAGA ACL writer inventory

This inventory separates atom ACL stamping from `SessionACL`. The latter
accumulates authority for session summaries in the `sessions` table; it does
not stamp atom rows.

## Atom creation paths

| Real entry point | Persisting call | ACL field sources | `origin_domain=None` | Finding |
|---|---|---|---|---|
| `memory_store` (`mimir/tools/store.py`) | `SagaStore.store` -> `saga.store.store` | owner and provenance from `get_provenance_from_auth_context`; channel, trigger, and ref from the immutable `AuthContext`; visibility is `service` for a trusted service and `private` otherwise; integrity comes from current IFC labels | Deliberate. The tool has no single source domain and service self-access uses the `service:{canonical}` owner. | Not defective. A real tool invocation stamps owner/channel/visibility/provenance and tests exercise the tool through its runtime carrier. |
| `saga_record_skill_learning` (`mimir/tools/saga_ops.py`) | `SagaStore.store` -> `saga.store.store` | owner and provenance from `get_provenance_from_auth_context`; channel from `AuthContext`; visibility from trusted-service status; integrity/trigger/ref use fail-closed primitive defaults | Deliberate for the same reason as `memory_store`: this agent-authored learning has no source-domain provenance. | Not defective. It stamps the available real provenance and does not invent a domain. |
| Production `SagaStore.consolidate` (`mimir/saga/client.py`) | `saga.store.store` | owner, channel, domain, visibility, and provenance are the fail-closed intersection of every evidence atom via `_compute_intersected_acl` | Deliberate only when inherited from common evidence or when ambiguous evidence collapses to the sentinel. | Derived path, not a direct stamping defect. Narrow legacy evidence produces a narrow legacy observation. Whether that inheritance policy should change belongs to #1115. |
| Standalone tier-2 `consolidate` (`mimir/saga/consolidate.py`) | `saga.store.store` | Same evidence intersection as production consolidation | Same as production consolidation. | Test/library path; not defective for this leaf. |
| Legacy importer `_migrate_atoms` (`mimir/saga/migrate.py`) | Direct `INSERT INTO atoms` | ACL columns are absent from supported legacy schemas, so schema defaults supply `legacy_admin`, null channel/domain, and empty provenance | Deliberate fail-closed handling of ownership that cannot be proved. | Not defective. Existing rows are not rewritten here. |
| LongMemEval benchmark ingest | `SagaStore.store` -> `saga.store.store` | Omits ACL arguments, receiving the primitive's sentinel defaults in a fresh benchmark-local database | Deliberate isolation for synthetic benchmark data. | Not a live production writer. |
| Direct `SagaStore.store` / `saga.store.store` library call | `saga.store.store` | Explicit ACL arguments pass through; omitted values become owner/visibility `legacy_admin`, null channel/domain, and empty provenance | Deliberate lower-layer fail-closed default because this API has no `AuthContext`. | Not defective. Widening this default would destroy the sentinel's role. Trusted application entry points must supply stamps. |

`saga_end_session` is not an atom writer. It writes `sessions`; its resource ACL
comes from accumulated `SessionACL`, independently of the synthesis service's
execution authority.

## Mutation paths

General and per-skill dedup (`mimir/saga/dedup.py`), explicit forget, and
consolidation rollback can update or tombstone atoms. They do not rewrite ACL
columns. Dedup partitions destructive merges by owner, domain, and visibility.

## Corpus classification

Run the read-only inventory against the live database:

```console
mimir saga-acl-inventory --db /path/to/.mimir/saga.db
```

The report counts:

- `direct_write_missing_owner`: live raw sentinel rows that nevertheless retain
  evidence of an attributable direct write: `agent_authored`/`skill_learning`
  source type, a channel/domain, or non-empty provenance. These are class (a)
  and require operator investigation by the grouped fields.
- `derived_legacy_inheritance`: live legacy observations with an
  `evidenced_by` relation. These definitively came through derived ACL
  inheritance and are class (b).
- `service_owned`: rows whose owner is `service:*`; this is class (c).
- `other_owned`: non-service rows with a proved non-sentinel owner.
- `legacy_unattributed`: raw sentinel rows with no persisted attribution. This
  includes the intentional legacy-import shape, so the database alone cannot
  honestly place these in class (a).
- `unclassified`: rows for which those persisted facts are insufficient, such
  as a legacy observation missing its evidence relation.

The classes are exhaustive and their counts sum to `total`. The tool does not
claim that every raw sentinel row is defective because atoms do not persist a
writer-path discriminator. `direct_legacy_detail` groups all raw sentinel rows
so deployment history can further resolve `legacy_unattributed`. The tool does
not mutate or migrate any row.

## Conclusion

No direct production tool writer is established as defective. Both real tool
entry points stamp every ACL value for which they have provenance and
deliberately leave the domain null rather than inventing one. Remaining
legacy-unattributed rows need deployment/import history to identify their writer;
derived legacy narrowness is explicitly deferred to #1115.
