"""LongMemEval through mimir's BenchBridge dispatch path.

Lives at workspace root, NOT under saga/, so the saga workspace package
stays mimir-independent. ``saga.benchmarks.longmemeval`` (saga-direct,
retrieval-only) keeps working standalone; this harness adds the
mimir-side hooks (pre_message, post_message, session_id,
contextual_rewrite, etc.) so retrieval changes can be measured
end-to-end through the full agent path.
"""
