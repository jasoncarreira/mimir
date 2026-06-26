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
    FileToolRouter,
    ReadOnlyFilesystemBackend,
    WriteGuardBackend,
    _RootAwareFilesystemBackend,
    build_file_tool_routes,
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

    def test_blocks_write_to_identities_yaml(self, home: Path) -> None:
        # state/ is writable, but identities.yaml (the auth identity + role
        # registry) is denied to the agent's file tools — a prompt-injected
        # chat user must not be able to grant themselves an admin role.
        b = WriteGuardBackend(root_dir=home, writable_dirs=["state"])
        r = b.write(file_path="/state/identities.yaml", content="people: []\n")
        assert "identities.yaml" in (getattr(r, "error", "") or "")

    def test_blocks_edit_to_identities_yaml(self, home: Path) -> None:
        (home / "state" / "identities.yaml").write_text("people: []\n")
        b = WriteGuardBackend(root_dir=home, writable_dirs=["state"])
        r = b.edit(
            file_path="/state/identities.yaml",
            old_string="people: []",
            new_string="people: [{canonical: x, access: {roles: [admin]}}]",
        )
        assert "identities.yaml" in (getattr(r, "error", "") or "")

    def test_blocks_upload_to_identities_yaml(self, home: Path) -> None:
        b = WriteGuardBackend(root_dir=home, writable_dirs=["state"])
        results = b.upload_files([("/state/identities.yaml", b"people: []\n")])
        assert results and results[0].error == "permission_denied"

    def test_allows_other_state_files(self, home: Path) -> None:
        # Only identities.yaml is protected; the rest of state/ stays writable
        # (e.g. the agent's own web_ui.json name/skin config).
        b = WriteGuardBackend(root_dir=home, writable_dirs=["state"])
        r = b.write(file_path="/state/web_ui.json", content="{}\n")
        assert getattr(r, "error", None) is None

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


    def test_drain_denials_can_scope_by_turn_id(self, home: Path) -> None:
        b = WriteGuardBackend(root_dir=home, writable_dirs=["state"])
        from mimir._context import reset_current_turn, set_current_turn
        from mimir.models import TurnContext

        ctx1 = TurnContext(
            turn_id="t-one",
            session_id="s-one",
            trigger="user_message",
            channel_id="discord-1",
            started_at=0.0,
        )
        tok1 = set_current_turn(ctx1)
        try:
            b.write(file_path="/logs/one.txt", content="no")
        finally:
            reset_current_turn(tok1)

        ctx2 = TurnContext(
            turn_id="t-two",
            session_id="s-two",
            trigger="user_message",
            channel_id="discord-2",
            started_at=0.0,
        )
        tok2 = set_current_turn(ctx2)
        try:
            b.edit(file_path="/logs/two.txt", old_string="x", new_string="y")
        finally:
            reset_current_turn(tok2)

        one = b.drain_denials(turn_id="t-one")
        assert [d["file_path"] for d in one] == ["/logs/one.txt"]
        assert b.drain_denials(turn_id="t-one") == []

        two = b.drain_denials(turn_id="t-two")
        assert [d["file_path"] for d in two] == ["/logs/two.txt"]
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
    """memory/core/ is read-only at runtime (chainlink #342).

    Layered on top of the per-directory writable-roots check: even when
    ``memory`` is in ``writable_dirs`` (the production default), writes
    under ``memory/core/`` are refused during ANY active turn — reflection
    included. Changes go through the core-memory PR proposal flow; only
    no-turn paths (the scaffold seed, ``mimir setup``, tests) may write.
    """

    @pytest.fixture
    def home_with_memory(self, tmp_path: Path) -> Path:
        (tmp_path / "state").mkdir()
        (tmp_path / "memory").mkdir()
        (tmp_path / "memory" / "core").mkdir()
        (tmp_path / "logs").mkdir()
        return tmp_path

    @staticmethod
    def _make_turn_ctx(trigger: str, channel_id: str, turn_id: str = "t-test"):
        """Build a minimal TurnContext for the gate check."""
        from mimir.models import TurnContext
        return TurnContext(
            turn_id=turn_id,
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
            assert "read-only" in (getattr(r, "error", "") or "")
        finally:
            self._clear_turn(tok)

    def test_allows_proposal_worktree_under_scratch(
        self, home_with_memory: Path
    ) -> None:
        """Change proposals (chainlink #339/#344) edit a worktree under
        scratch/. That path is NOT under home/memory/core, so the
        read-only gate must allow it even on a normal turn where a live
        memory/core/ write is refused — this is what lets the agent edit a
        proposal natively while live core stays protected."""
        wt_core = (
            home_with_memory
            / "scratch" / "proposals" / "proposal_x" / "memory" / "core"
        )
        wt_core.mkdir(parents=True)
        b = WriteGuardBackend(
            root_dir=home_with_memory, writable_dirs=["memory", "scratch"]
        )
        ctx = self._make_turn_ctx(trigger="user_message", channel_id="discord-1")
        tok = self._set_turn(ctx)
        try:
            blocked = b.write(file_path="/memory/core/00-persona.md", content="bad")
            assert "read-only" in (getattr(blocked, "error", "") or "")
            ok = b.write(
                file_path="/scratch/proposals/proposal_x/memory/core/00-persona.md",
                content="proposed",
            )
            assert not (getattr(ok, "error", "") or "")
            assert (wt_core / "00-persona.md").read_text() == "proposed"
        finally:
            self._clear_turn(tok)

    def test_blocks_prompts_write_and_points_at_proposal(
        self, home_with_memory: Path
    ) -> None:
        """prompts/ isn't a writable dir, so live writes are blocked — and the
        deny message points at the change-proposal flow (chainlink #344), not a
        generic 'not writable'. No active turn needed: this is the writable-root
        check, not the memory/core turn-gate."""
        b = WriteGuardBackend(root_dir=home_with_memory, writable_dirs=["memory", "state"])
        r = b.write(file_path="/prompts/reflect.md", content="x")
        err = getattr(r, "error", "") or ""
        assert "open_proposal" in err and "prompts/" in err
        e = b.edit(file_path="/prompts/reflect.md", old_string="a", new_string="b")
        assert "open_proposal" in (getattr(e, "error", "") or "")

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
            assert "read-only" in (getattr(r, "error", "") or "")
        finally:
            self._clear_turn(tok)

    def test_blocks_core_memory_write_in_reflection_turn(
        self, home_with_memory: Path
    ) -> None:
        """chainlink #342: reflection no longer has a core-write exception —
        even a reflection turn is blocked. Promotions go via the PR flow."""
        b = WriteGuardBackend(root_dir=home_with_memory, writable_dirs=["memory"])
        ctx = self._make_turn_ctx(
            trigger="scheduled_tick", channel_id="scheduler:reflect"
        )
        tok = self._set_turn(ctx)
        try:
            r = b.write(file_path="/memory/core/40-learned-behaviors.md",
                        content="nope")
            assert "read-only" in (getattr(r, "error", "") or "")
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
        """Bench / dev mode: pass ``enforce_core_memory_readonly=False`` to opt
        out of the gate. Other write protections (writable roots) still apply."""
        b = WriteGuardBackend(
            root_dir=home_with_memory,
            writable_dirs=["memory"],
            enforce_core_memory_readonly=False,
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
            assert "read-only" in (getattr(r, "error", "") or "")
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
            assert ("read-only" in err) or ("Write blocked" in err)
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
        assert denials[0]["op"] == "write_core_memory_readonly"
        assert "memory/core" in denials[0]["file_path"]

    def test_upload_to_core_memory_blocks_batch(
        self, home_with_memory: Path
    ) -> None:
        """Upload batches are atomic: if any path is core-memory-blocked,
        the whole batch fails."""
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
        assert "upload_core_memory_readonly" in ops


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


# ── configurable file-tool roots (#650) ──────────────────────────────────────


def _split_home(tmp_path: Path) -> Path:
    """A home that is a *subdir* of tmp_path, leaving room for sibling roots
    that are genuinely OUTSIDE the home."""
    h = tmp_path / "home"
    (h / "state").mkdir(parents=True)
    (h / "logs").mkdir()
    return h


class TestOutsideRootGuard:
    """``guard_outside_root`` turns the silent false-not-found (chainlink #650)
    into an actionable error, without disturbing home reads."""

    def test_guard_off_gives_no_actionable_message(self, tmp_path: Path) -> None:
        home = _split_home(tmp_path)
        outside = tmp_path / "outside.txt"
        outside.write_text("z\n")
        b = WriteGuardBackend(root_dir=home, writable_dirs=["state"])  # guard off
        r = b.read(file_path=str(outside))
        assert "outside the file-tool root" not in (getattr(r, "error", "") or "")

    def test_guard_on_clear_error_on_existing_outside_file(self, tmp_path: Path) -> None:
        home = _split_home(tmp_path)
        outside = tmp_path / "outside.txt"
        outside.write_text("z\n")
        b = WriteGuardBackend(root_dir=home, writable_dirs=["state"], guard_outside_root=True)
        r = b.read(file_path=str(outside))
        assert "outside the file-tool root" in (r.error or "")
        assert "MIMIR_FILE_TOOL_ROOTS" in (r.error or "")
        assert "shell_exec" in (r.error or "")

    def test_guard_on_ls_outside_clear_error(self, tmp_path: Path) -> None:
        home = _split_home(tmp_path)
        other = tmp_path / "other"
        other.mkdir()
        b = WriteGuardBackend(root_dir=home, writable_dirs=["state"], guard_outside_root=True)
        r = b.ls(path=str(other))
        assert "outside the file-tool root" in (r.error or "")

    def test_guard_on_allows_home_reads(self, tmp_path: Path) -> None:
        home = _split_home(tmp_path)
        (home / "state" / "s.txt").write_text("hi\n")
        b = WriteGuardBackend(root_dir=home, writable_dirs=["state"], guard_outside_root=True)
        # both the virtual form and the container-absolute home path resolve
        assert b.read(file_path="/state/s.txt").file_data["content"] == "hi\n"
        assert b.read(file_path=str(home / "state" / "s.txt")).file_data["content"] == "hi\n"

    def test_guard_only_fires_for_existing_paths(self, tmp_path: Path) -> None:
        home = _split_home(tmp_path)
        b = WriteGuardBackend(root_dir=home, writable_dirs=["state"], guard_outside_root=True)
        r = b.read(file_path=str(tmp_path / "ghost.txt"))  # outside but does NOT exist
        assert "outside the file-tool root" not in (getattr(r, "error", "") or "")


class TestBuildFileToolRoutes:
    def test_route_shapes_and_keys(self, tmp_path: Path) -> None:
        rw = tmp_path / "rw"
        rw.mkdir()
        ro = tmp_path / "ro"
        ro.mkdir()
        routes = build_file_tool_routes([(str(rw), "rw"), (str(ro), "ro")])
        assert set(routes) == {str(rw) + "/", str(ro) + "/"}
        assert isinstance(routes[str(rw) + "/"], _RootAwareFilesystemBackend)
        assert isinstance(routes[str(ro) + "/"], ReadOnlyFilesystemBackend)

    def test_grep_skips_vendor_and_vcs_subtrees(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "src").mkdir()
        (repo / "src" / "app.py").write_text("needle\n")
        (repo / "node_modules").mkdir()
        (repo / "node_modules" / "dep.py").write_text("needle\n")
        (repo / ".git").mkdir()
        (repo / ".git" / "packed-refs").write_text("needle\n")
        backend = _RootAwareFilesystemBackend(root_dir=repo, virtual_mode=True)

        result = backend.grep("needle", path="/")
        paths = {m["path"] for m in (result.matches or [])}

        assert "/src/app.py" in paths
        assert "/node_modules/dep.py" not in paths
        assert "/.git/packed-refs" not in paths

    def test_grep_caps_matches_without_result_error(self, tmp_path: Path, caplog) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "a.txt").write_text("needle\n")
        (repo / "b.txt").write_text("needle\n")
        backend = _RootAwareFilesystemBackend(
            root_dir=repo,
            virtual_mode=True,
            max_grep_matches=1,
        )

        result = backend.grep("needle", path="/")

        assert len(result.matches or []) == 1
        assert result.error is None
        assert "Grep truncated" in caplog.text

    def test_glob_skips_worktrees_and_caps_matches_without_result_error(
        self, tmp_path: Path, caplog,
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "src").mkdir()
        (repo / "src" / "a.py").write_text("x\n")
        (repo / "src" / "b.py").write_text("x\n")
        (repo / ".worktrees").mkdir()
        (repo / ".worktrees" / "ignored.py").write_text("x\n")
        backend = _RootAwareFilesystemBackend(
            root_dir=repo,
            virtual_mode=True,
            max_glob_matches=1,
        )

        result = backend.glob("**/*.py", path="/")
        paths = [m["path"] for m in (result.matches or [])]

        assert paths == ["/src/a.py"]
        assert result.error is None
        assert "Glob truncated" in caplog.text

    def test_glob_reports_scan_truncation_before_matching(self, tmp_path: Path, caplog) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "a.txt").write_text("x\n")
        (repo / "b.txt").write_text("x\n")
        (repo / "c.py").write_text("x\n")
        backend = _RootAwareFilesystemBackend(
            root_dir=repo,
            virtual_mode=True,
            max_scan_files=2,
        )

        result = backend.glob("**/*.py", path="/")

        assert result.matches == []
        assert result.error is None
        assert "scanned more than 2 files" in caplog.text

    def test_ls_hides_expensive_traversal_roots(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "src").mkdir()
        (repo / ".worktrees").mkdir()
        (repo / "node_modules").mkdir()
        backend = _RootAwareFilesystemBackend(root_dir=repo, virtual_mode=True)

        entries = backend.ls("/").entries or []
        names = {Path(e["path"].rstrip("/")).name for e in entries}

        assert "src" in names
        assert ".worktrees" not in names
        assert "node_modules" not in names


class TestFileToolRouter:
    @staticmethod
    def _router(tmp_path: Path):
        home = _split_home(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "x.py").write_text("CODE\n")
        ref = tmp_path / "ref"
        ref.mkdir()
        (ref / "r.md").write_text("REF\n")
        home_be = WriteGuardBackend(
            root_dir=home, writable_dirs=["state"], guard_outside_root=True,
        )
        router = FileToolRouter(
            default=home_be,
            routes=build_file_tool_routes([(str(repo), "rw"), (str(ref), "ro")]),
        )
        return home, repo, ref, router

    def test_rw_route_reads_and_writes_real_files(self, tmp_path: Path) -> None:
        _home, repo, _ref, router = self._router(tmp_path)
        assert router.read(f"{repo}/x.py").file_data["content"] == "CODE\n"
        w = router.write(f"{repo}/new.py", "Y\n")
        assert getattr(w, "error", None) is None
        assert (repo / "new.py").read_text() == "Y\n"

    def test_rw_route_symlink_escape_returns_clean_errors(self, tmp_path: Path) -> None:
        _home, repo, _ref, router = self._router(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        secret = outside / "secret.txt"
        secret.write_text("SECRET\n")
        (repo / "escape").symlink_to(outside, target_is_directory=True)

        r = router.read(f"{repo}/escape/secret.txt")
        assert "outside the file-tool root" in (r.error or "")
        assert r.file_data is None

        ls = router.ls(f"{repo}/escape")
        assert "outside the file-tool root" in (ls.error or "")
        assert ls.entries is None

        e = router.edit(f"{repo}/escape/secret.txt", old_string="SECRET", new_string="LEAK")
        assert "outside the file-tool root" in (e.error or "")
        assert secret.read_text() == "SECRET\n"

        w = router.write(f"{repo}/escape/new.txt", "LEAK\n")
        assert "outside the file-tool root" in (w.error or "")
        assert not (outside / "new.txt").exists()

    def test_ro_route_blocks_writes_allows_reads(self, tmp_path: Path) -> None:
        _home, _repo, ref, router = self._router(tmp_path)
        assert router.read(f"{ref}/r.md").file_data["content"] == "REF\n"
        w = router.write(f"{ref}/blocked.md", "no")
        assert "read-only" in (w.error or "")
        assert not (ref / "blocked.md").exists()

    def test_home_default_still_works(self, tmp_path: Path) -> None:
        _home, _repo, _ref, router = self._router(tmp_path)
        assert getattr(router.write("/state/s.txt", "hi"), "error", None) is None
        assert router.read("/state/s.txt").file_data["content"] == "hi"

    def test_out_of_all_roots_clear_error(self, tmp_path: Path) -> None:
        _home, _repo, _ref, router = self._router(tmp_path)
        outside = tmp_path / "outside.txt"
        outside.write_text("z\n")
        assert "outside the file-tool root" in (router.read(str(outside)).error or "")

    def test_drain_denials_forwards_to_home(self, tmp_path: Path) -> None:
        _home, _repo, _ref, router = self._router(tmp_path)
        # /logs is not a writable dir under the home → write denied + recorded
        router.write("/logs/x.txt", "no")
        assert any(d["op"] == "write" for d in router.drain_denials())

    @pytest.mark.asyncio
    async def test_broad_grep_keeps_default_and_route_matches_when_later_route_caps(
        self, tmp_path: Path,
    ) -> None:
        home = _split_home(tmp_path)
        (home / "state" / "home.txt").write_text("needle in home\n")
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "repo.txt").write_text("needle in repo\n")
        data = tmp_path / "data"
        data.mkdir()
        (data / "a.txt").write_text("needle in data a\n")
        (data / "b.txt").write_text("needle in data b\n")

        home_be = WriteGuardBackend(root_dir=home, writable_dirs=["state"])
        routes = build_file_tool_routes([(str(repo), "rw"), (str(data), "ro")])
        # Simulate a large data mount saturating its cap. Truncation must not be
        # returned as GrepResult.error, because deepagents CompositeBackend treats
        # route errors as fatal and discards matches already merged from home/repo.
        data_route = routes[str(data) + "/"]
        data_route._fs._max_grep_matches = 1
        router = FileToolRouter(default=home_be, routes=routes)

        result = await router.agrep("needle")
        paths = {m["path"] for m in (result.matches or [])}

        assert result.error is None
        assert "/state/home.txt" in paths
        assert f"{repo}/repo.txt" in paths
        assert len([p for p in paths if p.startswith(str(data))]) == 1
