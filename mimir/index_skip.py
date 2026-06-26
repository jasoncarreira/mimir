"""Shared file-search/state-index skip rules."""

from __future__ import annotations

from pathlib import Path

# Paths under state/ that look like operator/agent shared workspace, not
# knowledge worth retrieving via file_search. Embedding these is waste
# (frequent rewrites trigger reindexes) and pollution (results leak as
# "knowledge" hits).
INDEX_SKIP_PATHS: frozenset[str] = frozenset(
    {
        "state/heartbeat-backlog.md",  # operator/agent shared todo
        "state/proposed-changes.md",  # pending HITL items
        "state/identities.yaml",  # operator config; not .md but defensive
    }
)
INDEX_SKIP_PREFIXES: tuple[str, ...] = (
    # Poller working directories — non-content state (cursors, inboxes,
    # credentials, processed-message manifests). Nothing under here is
    # authored knowledge the agent should retrieve. Belt-and-suspenders
    # since the indexer is already .md-only and pollers write .json /
    # .yaml / .env, but protects against accidental .md drops (e.g. a
    # poller logging a notes file) and against future indexer expansion
    # to non-.md formats.
    "state/pollers/",
    # Social-CLI artifacts — operator-managed social graph / inbox
    # state; not authored knowledge the agent retrieves. Frequent writes
    # (per-message processed manifests, inbox snapshots) would cause
    # constant reindex churn on social-active installs.
    "state/social/",
)


def _normalize_skip_prefix(line: str) -> str | None:
    prefix = line.strip().replace("\\", "/")
    if not prefix or prefix.startswith("#"):
        return None
    prefix = prefix.removeprefix("./").lstrip("/")
    while "//" in prefix:
        prefix = prefix.replace("//", "/")
    parts = Path(prefix).parts
    if ".." in parts or prefix in {"", "."}:
        return None
    return prefix


def deployment_index_skip_prefixes(home: Path | None) -> tuple[str, ...]:
    """Read ``<home>/.mimir/index-skip.txt`` as home-relative prefixes."""
    if home is None:
        return ()
    path = home / ".mimir" / "index-skip.txt"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()
    prefixes = [
        prefix
        for line in lines
        if (prefix := _normalize_skip_prefix(line)) is not None
    ]
    return tuple(prefixes)


def is_index_skipped(rel: str, home: Path | None = None) -> bool:
    """Return whether a home-relative path is excluded from search/index."""
    return (
        rel in INDEX_SKIP_PATHS
        or any(rel.startswith(prefix) for prefix in INDEX_SKIP_PREFIXES)
        or any(rel.startswith(prefix) for prefix in deployment_index_skip_prefixes(home))
    )
