"""LongMemEval harness wired against ``mimir.saga.SagaStore``.

Parallel to ``benchmarks/longmemeval_via_mimir/`` (which routes through
mimir's full server + saga). This one bypasses saga entirely: per-
question SagaStore on a fresh mimir.saga.db, direct ingest +
consolidate + query, then saga's reader prompt synthesizes the
hypothesis from the retrieved atoms.

Existence rationale: lets us verify the new memory subsystem's
retrieval quality against LongMemEval without dragging in mimir's
dispatcher, agent loop, hooks, or cache effects — pure backend signal.
Once parity is established, the full-stack runner can flip to
SagaStore via ``make_saga_client(memory=True)``.
"""
