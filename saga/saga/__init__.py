"""SAGA — Multi-Stream Adaptive Memory (vestigial shell).

The runtime saga package that mimir's agent uses now lives at
``mimir.saga`` — an in-process SQLite-backed reimplementation that
landed during the saga-decoupling pass.

What survives under ``saga/saga/`` is just the LongMemEval benchmark
harness (``saga.benchmarks.longmemeval.{harness,config,ingest}``),
kept here so the bench runners under ``benchmarks/longmemeval_via_*/``
can still drive ingest + reader-prompt reuse against the upstream
dataset (``saga/external/longmemeval/``).

Use ``mimir.saga`` for application code; this package is a vendored
benchmark utility shell, not a memory backend.
"""

__version__ = "2026.05.19"
