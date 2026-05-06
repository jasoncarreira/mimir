"""Health probes consumed by the Self-state prompt block.

PR 4a introduces ``git_status_summary``: counts uncommitted files under
the home directory and surfaces the top few paths so the agent can
self-correct when commits start piling up (push outage, secret-scan
refusal, manual operator intervention left the tree dirty).

In PR 4a the function lives here but the rendered Self-state line is
NOT yet wired in — that's PR 4b's job (after the gitignore + setup flow
ensure ``/mimir-home`` is actually a git repo). On a non-init'd
``/mimir-home`` the function returns ``(0, [])`` so the eventual
caller in PR 4b sees a clean shape today.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


# Default for the Self-state line — operator-tunable later if 3 paths
# proves too noisy or too sparse.
DEFAULT_TOP_N = 3


def _porcelain_path(line: str) -> str:
    """Extract the path from a ``git status --porcelain`` line.

    Porcelain v1 format: 2 status chars + space + path. Tolerates the
    rename-form ``"R  old -> new"`` by taking the post-arrow side.
    """
    if len(line) < 3:
        return line.strip()
    path = line[3:].strip()
    if " -> " in path:
        path = path.split(" -> ", 1)[1]
    return path


def git_status_summary(
    home: Path,
    *,
    top_n: int = DEFAULT_TOP_N,
) -> tuple[int, list[str]]:
    """Return ``(count, top_paths)`` for the Self-state ``uncommitted in
    /mimir-home: …`` line.

    - ``count`` is the total number of uncommitted-or-untracked tracked-
      eligible paths reported by ``git status --porcelain``. Zero when
      ``home/.git`` doesn't exist (un-init'd repo — pre-PR-4b state).
    - ``top_paths`` is the first ``top_n`` paths in lex order, with a
      ``"…+N"`` suffix appended if more were truncated. The truncation
      sentinel is a STRING entry in the list; the count remains the
      true count, not the rendered length.
    - Synchronous because it's called from the prompt-render path,
      which is itself synchronous in mimir today; ``subprocess.run``
      blocks the caller for ~5-10ms.

    Returns ``(0, [])`` on any failure (missing .git, git not on PATH,
    timeout). Failures are debug-logged but never surfaced — this is a
    UI hint, not a load-bearing health probe.
    """
    git_dir = home / ".git"
    if not git_dir.exists():
        # PR 4a may run before ``mimir setup`` has init'd /mimir-home.
        # Returning (0, []) keeps the eventual PR 4b call site simple
        # and means the Self-state line stays blank pre-init.
        return (0, [])

    try:
        # ``--untracked-files=all`` expands an untracked directory into
        # its individual files. Without it, ``memory/`` with 12 new
        # files reads as a single ``?? memory/`` entry — which would
        # under-report the actual count for the Self-state line.
        result = subprocess.run(
            [
                "git", "-C", str(home),
                "status", "--porcelain", "--untracked-files=all",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        log.debug("git_status_summary failed: %s", exc)
        return (0, [])

    if result.returncode != 0:
        log.debug(
            "git_status_summary nonzero: rc=%d stderr=%r",
            result.returncode,
            result.stderr,
        )
        return (0, [])

    paths = [
        _porcelain_path(line)
        for line in result.stdout.splitlines()
        if line.strip()
    ]
    paths.sort()
    count = len(paths)
    if count == 0:
        return (0, [])

    if count <= top_n:
        return (count, paths)
    head = paths[:top_n]
    head.append(f"…+{count - top_n}")
    return (count, head)


__all__: tuple[str, ...] = (
    "DEFAULT_TOP_N",
    "git_status_summary",
)
