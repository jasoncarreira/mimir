"""LongMemEval through mimir's BenchBridge dispatch path (v0.5 §3).

Lives at workspace root, NOT under saga/, so the saga package stays
mimir-independent. saga.benchmarks.longmemeval (saga-direct, retrieval
only) keeps working standalone; this harness adds the mimir-side hooks
(pre_message, post_message, session_id, contextual_rewrite, etc.) so
v0.6+ retrieval changes can be measured end-to-end.

See V0.5.md §3 for the full design rationale.
"""
