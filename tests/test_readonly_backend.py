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

    def test_read_with_container_absolute_path(self, home: Path) -> None:
        # Agents in muninn-mimir frequently see container-absolute paths
        # (e.g. /mimir-home/state/x.md) in shell output and feedback
        # signals, then call read_file with that exact path. Upstream's
        # virtual_mode=True double-prefixes the path; the
        # _RootAwareFilesystemBackend strips the cwd prefix so both
        # forms resolve to the same file. Regression for turn
        # 1da3c007b611 where 4 read_file calls failed against existing
        # files because the agent passed the absolute container path.
        b = WriteGuardBackend(root_dir=home, writable_dirs=["state"])
        absolute = f"{home}/logs/existing.txt"
        result = b.read(file_path=absolute)
        text = getattr(result, "content", None) or str(result)
        assert "preexisting" in text

    def test_write_with_container_absolute_path(self, home: Path) -> None:
        # Writes via the container-absolute form must reach the same
        # file as the virtual form. Without the prefix-strip, the write
        # would land at <home>/<home>/state/x.txt — outside the writable
        # root, so it would error AND/OR write to the wrong place.
        b = WriteGuardBackend(root_dir=home, writable_dirs=["state"])
        absolute = f"{home}/state/from-absolute.txt"
        r = b.write(file_path=absolute, content="ok")
        assert getattr(r, "error", None) is None
        assert (home / "state" / "from-absolute.txt").read_text() == "ok"

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


class TestCoreMemoryReflectionGate:
    """S5-2 — memory/core/ writes are reflection-turn-only by policy.

    Layered on top of the per-directory writable-roots check: even when
    ``memory`` is in ``writable_dirs`` (the production default), writes
    under ``memory/core/`` are refused unless an active ``TurnContext``
    declares ``trigger == "scheduled_tick"`` AND ``channel_id`` starts
    with ``"scheduler:reflect"``.
    """

    @pytest.fixture
    def home_with_memory(self, tmp_path: Path) -> Path:
        (tmp_path / "state").mkdir()
        (tmp_path / "memory").mkdir()
        (tmp_path / "memory" / "core").mkdir()
        (tmp_path / "logs").mkdir()
        return tmp_path

    @staticmethod
    def _make_turn_ctx(trigger: str, channel_id: str):
        """Build a minimal TurnContext for the gate check. The backend
        only reads ``.trigger`` and ``.channel_id``, so a partial dataclass
        construction is fine."""
        from mimir.models import TurnContext
        return TurnContext(
            turn_id="t-test",
            session_id="s-test",
            trigger=trigger,
            channel_id=channel_id,
            started_at=0.0,
        )

    @staticmethod
    def _set_turn(ctx):
        from mimir._context import set_current_turn
        return set_current_turn(ctx)

    @staticmethod
    def _clear_turn(token):
        from mimir._context import reset_current_turn
        reset_current_turn(token)

    def test_blocks_core_memory_write_in_user_message_turn(
        self, home_with_memory: Path
    ) -> None:
        b = WriteGuardBackend(root_dir=home_with_memory, writable_dirs=["memory"])
        ctx = self._make_turn_ctx(trigger="user_message", channel_id="discord-123")
        tok = self._set_turn(ctx)
        try:
            r = b.write(file_path="/memory/core/00-persona.md", content="bad")
            assert "reflection-only" in (getattr(r, "error", "") or "")
        finally:
            self._clear_turn(tok)

    def test_blocks_core_memory_write_in_heartbeat_turn(
        self, home_with_memory: Path
    ) -> None:
        """Heartbeat is scheduled_tick BUT on scheduler:heartbeat, not
        scheduler:reflect — must not slip through."""
        b = WriteGuardBackend(root_dir=home_with_memory, writable_dirs=["memory"])
        ctx = self._make_turn_ctx(
            trigger="scheduled_tick", channel_id="scheduler:heartbeat"
        )
        tok = self._set_turn(ctx)
        try:
            r = b.write(file_path="/memory/core/40-learned-behaviors.md",
                        content="bad")
            assert "reflection-only" in (getattr(r, "error", "") or "")
        finally:
            self._clear_turn(tok)

    def test_allows_core_memory_write_in_reflection_turn(
        self, home_with_memory: Path
    ) -> None:
        b = WriteGuardBackend(root_dir=home_with_memory, writable_dirs=["memory"])
        ctx = self._make_turn_ctx(
            trigger="scheduled_tick", channel_id="scheduler:reflect"
        )
        tok = self._set_turn(ctx)
        try:
            r = b.write(file_path="/memory/core/40-learned-behaviors.md",
                        content="ok")
            assert getattr(r, "error", None) is None
        finally:
            self._clear_turn(tok)

    def test_allows_core_memory_write_when_no_turn_active(
        self, home_with_memory: Path
    ) -> None:
        """Backend tests, ``mimir setup``, and non-turn cron callables
        write outside any TurnContext. The gate must not block them."""
        b = WriteGuardBackend(root_dir=home_with_memory, writable_dirs=["memory"])
        # No turn set — _current_turn is None.
        r = b.write(file_path="/memory/core/00-persona.md", content="ok")
        assert getattr(r, "error", None) is None

    def test_allows_core_memory_write_when_gate_disabled(
        self, home_with_memory: Path
    ) -> None:
        """Bench / dev mode: pass ``enforce_core_memory_reflection_only=False``
        to opt out of the S5-2 gate. Other write protections (writable
        roots) still apply."""
        b = WriteGuardBackend(
            root_dir=home_with_memory,
            writable_dirs=["memory"],
            enforce_core_memory_reflection_only=False,
        )
        ctx = self._make_turn_ctx(trigger="user_message", channel_id="discord-123")
        tok = self._set_turn(ctx)
        try:
            r = b.write(file_path="/memory/core/00-persona.md", content="ok")
            assert getattr(r, "error", None) is None
        finally:
            self._clear_turn(tok)

    def test_edit_to_core_memory_gated_same_as_write(
        self, home_with_memory: Path
    ) -> None:
        # Seed a file inside core so Edit has something to operate on.
        (home_with_memory / "memory" / "core" / "00-persona.md").write_text(
            "original\n"
        )
        b = WriteGuardBackend(root_dir=home_with_memory, writable_dirs=["memory"])
        ctx = self._make_turn_ctx(trigger="user_message", channel_id="discord-123")
        tok = self._set_turn(ctx)
        try:
            r = b.edit(
                file_path="/memory/core/00-persona.md",
                old_string="original",
                new_string="bad",
            )
            assert "reflection-only" in (getattr(r, "error", "") or "")
        finally:
            self._clear_turn(tok)

    def test_writes_to_memory_outside_core_unaffected(
        self, home_with_memory: Path
    ) -> None:
        """memory/learnings-pending.md is under memory/, NOT memory/core/.
        The gate must not over-reach to sibling subtrees."""
        b = WriteGuardBackend(root_dir=home_with_memory, writable_dirs=["memory"])
        ctx = self._make_turn_ctx(trigger="user_message", channel_id="discord-123")
        tok = self._set_turn(ctx)
        try:
            r = b.write(
                file_path="/memory/learnings-pending.md", content="entry"
            )
            assert getattr(r, "error", None) is None
        finally:
            self._clear_turn(tok)

    def test_traversal_into_core_via_relative_path_blocked(
        self, home_with_memory: Path
    ) -> None:
        """An agent that smuggles ``../core/foo.md`` from inside memory/
        must NOT slip past the gate. ``_resolve_target`` rejects any
        path whose ``.parts`` contains ``..`` (lexical traversal guard
        in the existing writable-roots check), so this case is blocked
        at the writable-roots layer before the core-memory gate runs.
        Either block reason is acceptable — the assertion is just
        \"this write must NOT succeed.\""""
        b = WriteGuardBackend(root_dir=home_with_memory, writable_dirs=["memory"])
        ctx = self._make_turn_ctx(trigger="user_message", channel_id="discord-123")
        tok = self._set_turn(ctx)
        try:
            r = b.write(
                file_path="/memory/sub/../core/00-persona.md",
                content="bad",
            )
            err = getattr(r, "error", "") or ""
            assert err  # must be blocked
            assert ("reflection-only" in err) or ("Write blocked" in err)
        finally:
            self._clear_turn(tok)

    def test_denial_recorded_for_core_memory_block(
        self, home_with_memory: Path
    ) -> None:
        """The blocked write must appear in ``drain_denials()`` with a
        distinct ``op`` so the turn viewer can show what was attempted."""
        b = WriteGuardBackend(root_dir=home_with_memory, writable_dirs=["memory"])
        ctx = self._make_turn_ctx(trigger="user_message", channel_id="discord-123")
        tok = self._set_turn(ctx)
        try:
            b.write(file_path="/memory/core/00-persona.md", content="bad")
        finally:
            self._clear_turn(tok)
        denials = b.drain_denials()
        assert len(denials) == 1
        assert denials[0]["op"] == "write_core_memory_non_reflection"
        assert "memory/core" in denials[0]["file_path"]

    def test_upload_to_core_memory_blocks_batch(
        self, home_with_memory: Path
    ) -> None:
        """Upload batches are atomic: if any path is core-memory-blocked
        in a non-reflection turn, the whole batch fails."""
        b = WriteGuardBackend(root_dir=home_with_memory, writable_dirs=["memory"])
        ctx = self._make_turn_ctx(trigger="user_message", channel_id="discord-123")
        tok = self._set_turn(ctx)
        try:
            results = b.upload_files([
                ("/memory/learnings-pending.md", b"entry"),
                ("/memory/core/40-learned-behaviors.md", b"bad"),
            ])
        finally:
            self._clear_turn(tok)
        # Both entries fail because the batch is atomic.
        assert all(getattr(r, "error", None) == "permission_denied"
                   for r in results)
        # And the denial trail records the core-memory-specific op.
        denials = b.drain_denials()
        ops = [d["op"] for d in denials]
        assert "upload_core_memory_non_reflection" in ops


class TestOnboardingMode:
    """S5-2 onboarding bypass via the ``onboarding_mode`` constructor
    flag (sourced from ``MIMIR_ONBOARDING_MODE`` env var in
    production). When True, the reflection-only gate on
    ``memory/core/`` yields so the agent can collaboratively bootstrap
    its persona / memory architecture during first-run setup.

    Restart-to-flip is the tamper-resistance property: the flag is
    read once at backend construction; mid-process env changes don't
    re-evaluate. The agent can edit compose.env via bash but the
    value isn't read until the next container restart, and the agent
    has no docker socket to self-restart.
    """

    @pytest.fixture
    def home_with_memory(self, tmp_path: Path) -> Path:
        (tmp_path / "state").mkdir()
        (tmp_path / "memory").mkdir()
        (tmp_path / "memory" / "core").mkdir()
        return tmp_path

    @staticmethod
    def _make_user_turn_ctx():
        from mimir.models import TurnContext
        return TurnContext(
            turn_id="t-test", session_id="s-test", trigger="user_message",
            channel_id="discord-123", started_at=0.0,
        )

    @staticmethod
    def _set_turn(ctx):
        from mimir._context import set_current_turn
        return set_current_turn(ctx)

    @staticmethod
    def _clear_turn(token):
        from mimir._context import reset_current_turn
        reset_current_turn(token)

    def test_bypass_active_when_onboarding_mode_true(
        self, home_with_memory: Path
    ) -> None:
        """With onboarding_mode=True, non-reflection writes to
        memory/core/ succeed — the bootstrap path."""
        b = WriteGuardBackend(
            root_dir=home_with_memory, writable_dirs=["memory"],
            onboarding_mode=True,
        )
        ctx = self._make_user_turn_ctx()
        tok = self._set_turn(ctx)
        try:
            r = b.write(file_path="/memory/core/00-persona.md", content="bootstrap")
            assert getattr(r, "error", None) is None
        finally:
            self._clear_turn(tok)

    def test_gate_enforced_when_onboarding_mode_false(
        self, home_with_memory: Path
    ) -> None:
        """Default ``onboarding_mode=False`` → gate enforces in
        non-reflection turns. The production steady-state."""
        b = WriteGuardBackend(
            root_dir=home_with_memory, writable_dirs=["memory"],
            onboarding_mode=False,
        )
        ctx = self._make_user_turn_ctx()
        tok = self._set_turn(ctx)
        try:
            r = b.write(file_path="/memory/core/00-persona.md", content="bad")
            assert "reflection-only" in (getattr(r, "error", "") or "")
        finally:
            self._clear_turn(tok)

    def test_default_constructor_arg_is_false(
        self, home_with_memory: Path
    ) -> None:
        """Omitting onboarding_mode= falls back to False — fail-closed
        default. A backend constructed without the flag enforces the
        gate (matches the steady-state production shape)."""
        b = WriteGuardBackend(
            root_dir=home_with_memory, writable_dirs=["memory"],
            # No onboarding_mode arg.
        )
        ctx = self._make_user_turn_ctx()
        tok = self._set_turn(ctx)
        try:
            r = b.write(file_path="/memory/core/00-persona.md", content="bad")
            assert "reflection-only" in (getattr(r, "error", "") or "")
        finally:
            self._clear_turn(tok)

    def test_reflection_turn_unaffected_by_onboarding_mode(
        self, home_with_memory: Path
    ) -> None:
        """Reflection turns always pass the gate, regardless of
        onboarding_mode. The bypass is purely additive."""
        from mimir.models import TurnContext
        # onboarding_mode=False — reflection turn should still write.
        b = WriteGuardBackend(
            root_dir=home_with_memory, writable_dirs=["memory"],
            onboarding_mode=False,
        )
        ctx = TurnContext(
            turn_id="t", session_id="s", trigger="scheduled_tick",
            channel_id="scheduler:reflect", started_at=0.0,
        )
        tok = self._set_turn(ctx)
        try:
            r = b.write(file_path="/memory/core/40-learned-behaviors.md", content="ok")
            assert getattr(r, "error", None) is None
        finally:
            self._clear_turn(tok)

    def test_onboarding_mode_does_not_unlock_writable_roots(
        self, home_with_memory: Path
    ) -> None:
        """The bypass only affects the S5-2 memory/core/ gate. Writes
        outside MIMIR_FOLDERS are still blocked by the standard
        writable-roots check."""
        (home_with_memory / "logs").mkdir()
        b = WriteGuardBackend(
            root_dir=home_with_memory, writable_dirs=["memory"],
            onboarding_mode=True,  # bypass on
        )
        # logs/ is not in writable_dirs → blocked by the outer check,
        # not by the S5-2 gate.
        r = b.write(file_path="/logs/exfil.txt", content="nope")
        assert "Writable directories" in (getattr(r, "error", "") or "")

    def test_onboarding_mode_does_not_affect_non_core_paths(
        self, home_with_memory: Path
    ) -> None:
        """Writes to non-core memory/ paths (e.g.
        memory/learnings-pending.md) work the same whether
        onboarding_mode is on or off — they were never gated."""
        b = WriteGuardBackend(
            root_dir=home_with_memory, writable_dirs=["memory"],
            onboarding_mode=False,
        )
        ctx = self._make_user_turn_ctx()
        tok = self._set_turn(ctx)
        try:
            r = b.write(
                file_path="/memory/learnings-pending.md", content="entry",
            )
            assert getattr(r, "error", None) is None
        finally:
            self._clear_turn(tok)

    def test_onboarding_mode_true_logs_warning(
        self, home_with_memory: Path, caplog
    ) -> None:
        """Construction with ``onboarding_mode=True`` must emit a
        WARNING log so the operator sees "I'm in onboarding mode" on
        every container restart. Reviewer note 3 on PR #301."""
        import logging
        with caplog.at_level(logging.WARNING, logger="mimir.readonly_backend"):
            WriteGuardBackend(
                root_dir=home_with_memory, writable_dirs=["memory"],
                onboarding_mode=True,
            )
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any(
            "MIMIR_ONBOARDING_MODE=true" in r.getMessage() for r in warnings
        ), "expected an onboarding-mode WARNING; got: " + ", ".join(
            r.getMessage() for r in warnings
        )

    def test_onboarding_mode_false_does_not_log_warning(
        self, home_with_memory: Path, caplog
    ) -> None:
        """Default state (bypass off) must NOT emit the warning —
        otherwise every container restart would log a misleading line."""
        import logging
        with caplog.at_level(logging.WARNING, logger="mimir.readonly_backend"):
            WriteGuardBackend(
                root_dir=home_with_memory, writable_dirs=["memory"],
                onboarding_mode=False,
            )
        assert not any(
            "MIMIR_ONBOARDING_MODE" in r.getMessage()
            for r in caplog.records if r.levelno == logging.WARNING
        )


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
