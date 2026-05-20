"""Unit tests for ``mimir.readonly_backend``.

Covers the per-directory write enforcement that ``WriteGuardBackend``
applies on top of deepagents' ``FilesystemBackend``, plus the
``ReadOnlyFilesystemBackend`` blanket-block variant. Reads stay
unrestricted on both, by design — file_search and Grep have to keep
working against the full home tree.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mimir.readonly_backend import (
    ReadOnlyFilesystemBackend,
    WriteGuardBackend,
)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """Synthetic mimir home with state/, logs/, and .mimir/ subdirs."""
    (tmp_path / "state").mkdir()
    (tmp_path / "memory").mkdir()
    (tmp_path / "logs").mkdir()
    (tmp_path / ".mimir").mkdir()
    (tmp_path / "logs" / "existing.txt").write_text("preexisting log line\n")
    return tmp_path


class TestWriteGuardBackend:
    def test_allows_write_to_writable_root(self, home: Path) -> None:
        b = WriteGuardBackend(root_dir=home, writable_dirs=["state"])
        r = b.write(file_path="/state/note.txt", content="hi")
        assert getattr(r, "error", None) is None

    def test_allows_write_to_nested_path_under_writable_root(self, home: Path) -> None:
        b = WriteGuardBackend(root_dir=home, writable_dirs=["state"])
        r = b.write(file_path="/state/sub/dir/note.txt", content="hi")
        assert getattr(r, "error", None) is None

    def test_blocks_write_to_non_writable_dir(self, home: Path) -> None:
        b = WriteGuardBackend(root_dir=home, writable_dirs=["state"])
        r = b.write(file_path="/logs/bad.txt", content="hi")
        assert "Write blocked" in (getattr(r, "error", "") or "")

    def test_blocks_write_to_implicit_dir(self, home: Path) -> None:
        # .mimir/ is not in writable_dirs; saga db must not be writable
        # via deepagents Write tool.
        b = WriteGuardBackend(root_dir=home, writable_dirs=["state"])
        r = b.write(file_path="/.mimir/db.sqlite", content="hi")
        assert "Write blocked" in (getattr(r, "error", "") or "")

    def test_reads_unrestricted(self, home: Path) -> None:
        # Read tools must NOT be path-restricted — file_search and Grep
        # operate over the whole home, including ro dirs.
        b = WriteGuardBackend(root_dir=home, writable_dirs=["state"])
        result = b.read(file_path="/logs/existing.txt")
        # deepagents 0.6 wraps reads in a ReadResult; surface the content
        # via str() / .content depending on the version.
        text = getattr(result, "content", None) or str(result)
        assert "preexisting" in text

    def test_blocks_edit_outside_writable_root(self, home: Path) -> None:
        b = WriteGuardBackend(root_dir=home, writable_dirs=["state"])
        r = b.edit(
            file_path="/logs/existing.txt",
            old_string="preexisting",
            new_string="rewritten",
        )
        assert "Edit blocked" in (getattr(r, "error", "") or "")

    def test_normalizes_leading_slashes(self, home: Path) -> None:
        # writable_dirs entries can be passed with or without leading
        # slash; both should match.
        for i, root in enumerate(("state", "/state", "state/")):
            b = WriteGuardBackend(root_dir=home, writable_dirs=[root])
            r = b.write(file_path=f"/state/x{i}.txt", content="hi")
            assert getattr(r, "error", None) is None

    def test_upload_files_partial_block(self, home: Path) -> None:
        b = WriteGuardBackend(root_dir=home, writable_dirs=["state"])
        results = b.upload_files([
            ("/state/ok.txt", b"a"),
            ("/logs/blocked.txt", b"b"),
        ])
        # Any blocked path → entire batch is rejected (atomic semantics).
        # Every input gets a ``permission_denied`` response so the caller
        # can tell nothing was uploaded; allowed paths intentionally
        # surface the same error rather than ambiguous silent success.
        errors = [getattr(r, "error", None) for r in results]
        assert errors == ["permission_denied", "permission_denied"]

    def test_blocks_dotdot_traversal(self, home: Path) -> None:
        # PurePosixPath alone doesn't collapse ``..`` — without explicit
        # rejection, ``/state/../logs/evil.txt`` would have ``/state``
        # in path.parents and slipped through.
        b = WriteGuardBackend(root_dir=home, writable_dirs=["state"])
        r = b.write(file_path="/state/../logs/evil.txt", content="no")
        assert "Write blocked" in (getattr(r, "error", "") or "")

    def test_blocks_dotdot_nested(self, home: Path) -> None:
        b = WriteGuardBackend(root_dir=home, writable_dirs=["state"])
        r = b.write(file_path="state/sub/../../.mimir/db.sqlite", content="no")
        assert "Write blocked" in (getattr(r, "error", "") or "")

    def test_blocks_absolute_path_outside_home(self, home: Path) -> None:
        b = WriteGuardBackend(root_dir=home, writable_dirs=["state"])
        # Leading slash strips → resolved under home, so ``/etc/passwd``
        # becomes ``<home>/etc/passwd`` which isn't in any writable root.
        r = b.write(file_path="/etc/passwd", content="no")
        assert "Write blocked" in (getattr(r, "error", "") or "")

    def test_blocks_symlink_escape(self, home: Path) -> None:
        # A symlink from inside a writable root pointing OUTSIDE
        # the writable root must be blocked — even though the visible
        # path passes the lexical check, ``Path.resolve()`` follows the
        # link and the target lands outside.
        target_dir = home / "logs"
        link = home / "state" / "escape"
        link.symlink_to(target_dir, target_is_directory=True)
        b = WriteGuardBackend(root_dir=home, writable_dirs=["state"])
        r = b.write(file_path="/state/escape/evil.txt", content="no")
        assert "Write blocked" in (getattr(r, "error", "") or "")

    def test_allows_internal_symlink(self, home: Path) -> None:
        # A symlink that points back into the same writable root is
        # fine — the resolved target is still under ``state``.
        (home / "state" / "sub").mkdir()
        link = home / "state" / "alias"
        link.symlink_to(home / "state" / "sub", target_is_directory=True)
        b = WriteGuardBackend(root_dir=home, writable_dirs=["state"])
        r = b.write(file_path="/state/alias/note.txt", content="ok")
        assert getattr(r, "error", None) is None

    def test_prefix_collision_does_not_grant_access(self, home: Path) -> None:
        # writable_dirs=["state"] must NOT match ``state-backup/`` — the
        # lexical prefix string ``state`` is a prefix of ``state-backup``
        # but ``state-backup`` is a sibling, not a descendant.
        (home / "state-backup").mkdir()
        b = WriteGuardBackend(root_dir=home, writable_dirs=["state"])
        r = b.write(file_path="/state-backup/x.txt", content="no")
        assert "Write blocked" in (getattr(r, "error", "") or "")

    def test_rejects_dot_writable_dir(self, home: Path) -> None:
        # A bogus folder spec ``.:rw`` (or empty after strip) used to
        # alias the root and make everything writable. We log + drop it.
        b = WriteGuardBackend(root_dir=home, writable_dirs=[".", "..", "", "state"])
        # Only ``state`` should survive.
        assert len(b._writable_roots) == 1
        r = b.write(file_path="/.mimir/db.sqlite", content="no")
        assert "Write blocked" in (getattr(r, "error", "") or "")

    def test_drain_denials_captures_blocked_writes(self, home: Path) -> None:
        b = WriteGuardBackend(root_dir=home, writable_dirs=["state"])
        # Pre-fix permission_denials in TurnRecord was always empty —
        # the SDK reported WriteGuard refusals via that field, but the
        # deepagents cutover dropped the capture path. Now blocked
        # write/edit/upload land in self._denials and run_turn drains
        # them into the TurnRecord at end of turn.
        b.write(file_path="/logs/blocked.txt", content="no")
        b.edit(file_path="/logs/existing.txt", old_string="x", new_string="y")
        b.upload_files([("/logs/up.txt", b"x")])
        denials = b.drain_denials()
        assert len(denials) == 3
        ops = sorted(d["op"] for d in denials)
        assert ops == ["edit", "upload", "write"]
        # Drain clears, so the next call returns nothing.
        assert b.drain_denials() == []

    def test_denials_not_recorded_on_allowed_writes(self, home: Path) -> None:
        b = WriteGuardBackend(root_dir=home, writable_dirs=["state"])
        b.write(file_path="/state/ok.txt", content="hi")
        assert b.drain_denials() == []

    def test_explicit_allowlist_blocks_unknown_method(self, home: Path) -> None:
        # __getattr__ no longer passes through arbitrary attribute
        # access — only methods on _ALLOWED_READS forward. A future
        # deepagents release adding ``delete_file`` must AttributeError
        # until we audit and wrap it.
        b = WriteGuardBackend(root_dir=home, writable_dirs=["state"])
        with pytest.raises(AttributeError):
            b.some_future_mutator  # noqa: B018
        # Known read methods still forward.
        assert callable(b.read)
        assert callable(b.ls_info)

    def test_deepagents_06_async_fs_methods_forward(self, home: Path) -> None:
        """deepagents 0.6+ exposes high-level ``als`` / ``agrep`` /
        ``aglob`` wrappers (return tool-friendly result types) on top
        of the pre-0.6 low-level ``*_info`` / ``*_raw`` variants. The
        agent's filesystem tools call the high-level names. Pre-fix,
        ``WriteGuardBackend`` allowlisted only the low-level variants,
        so an agent on deepagents 0.6+ hit ``AttributeError:
        WriteGuardBackend does not forward 'agrep'`` on any grep call.

        Caught during muninn-mimir cutover 2026-05-20 (the heartbeat
        skill called ``agrep`` and crashed).

        All six methods are read-only (audited against
        ``deepagents/backends/composite.py``); allowlisting them is
        safe."""
        b = WriteGuardBackend(root_dir=home, writable_dirs=["state"])
        # Sync read wrappers
        assert callable(b.ls)
        assert callable(b.grep)
        assert callable(b.glob)
        # Async read wrappers (the ones the agent typically uses)
        assert callable(b.als)
        assert callable(b.agrep)
        assert callable(b.aglob)
        # Existing low-level variants still forward (back-compat).
        assert callable(b.ls_info)
        assert callable(b.grep_raw)
        assert callable(b.aglob_info)


class TestReadOnlyFilesystemBackend:
    def test_blocks_all_writes(self, home: Path) -> None:
        b = ReadOnlyFilesystemBackend(root_dir=home)
        r = b.write(file_path="/state/anywhere.txt", content="no")
        assert "read-only" in (getattr(r, "error", "") or "")

    def test_blocks_all_edits(self, home: Path) -> None:
        b = ReadOnlyFilesystemBackend(root_dir=home)
        r = b.edit(file_path="/logs/existing.txt", old_string="preexisting", new_string="x")
        assert "read-only" in (getattr(r, "error", "") or "")

    def test_blocks_uploads(self, home: Path) -> None:
        b = ReadOnlyFilesystemBackend(root_dir=home)
        results = b.upload_files([("/state/x.txt", b"a")])
        assert getattr(results[0], "error", None) == "permission_denied"

    def test_reads_still_work(self, home: Path) -> None:
        b = ReadOnlyFilesystemBackend(root_dir=home)
        result = b.read(file_path="/logs/existing.txt")
        text = getattr(result, "content", None) or str(result)
        assert "preexisting" in text
