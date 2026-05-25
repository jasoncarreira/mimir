"""saga — LongMemEval bench harness package.

The runtime memory backend mimir uses lives at ``mimir.saga`` (an
in-process SQLite-backed implementation, part of the ``mimir-agent``
package). This ``saga`` package — at ``benchmarks/saga/`` in the
repo — is unrelated to that runtime: it's a separate workspace
member that holds the shared LongMemEval bench harness
(``saga.benchmarks.longmemeval.{harness,config,ingest}``),
imported by the bench runners under ``benchmarks/longmemeval_via_*/``
to drive ingest + reader-prompt reuse against the upstream dataset
(``benchmarks/saga/external/longmemeval/``).

Use ``mimir.saga`` for application code; ``saga`` (this package) is a
benchmark-only utility.
"""

__version__ = "2026.05.19"
