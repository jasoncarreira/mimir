"""Unit tests for ``mimir.readonly_backend``.

Covers the per-directory write enforcement that ``WriteGuardBackend``
applies on top of deepagents' ``FilesystemBackend``, plus the
``ReadOnlyFilesystemBackend`` blanket-block variant. Reads stay
unrestricted on both, by design — file_search and Grep have to keep
working against the full home tree.
"""

from __future__ import annotations

import inspect
import json
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from pathlib import Path
from types import SimpleNamespace

import pytest
from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.skills import SkillsMiddleware
from langchain_core.messages import ToolMessage

from mimir._context import reset_current_turn, set_current_turn
from mimir.access_control import (
    OperationDecision,
    ServicePrincipal,
    ToolAuthorization,
    ToolFlowDirection,
    begin_protected_result_capture,
    classify_protected_result,
    end_protected_result_capture,
)
from mimir.models import AuthContext
from mimir.read_policy import framework_large_tool_results_root
from mimir.readonly_backend import (
    MAX_GREP_CONTEXT_LINES,
    FileToolRouter,
    MimirFilesystemMiddleware,
    ReadOnlyFilesystemBackend,
    WriteGuardBackend,
    _RootAwareFilesystemBackend,
    build_file_tool_routes,
)


class _ScandirEntries(AbstractContextManager):
    def __init__(self, entries):
        self._entries = entries

    def __iter__(self):
        return iter(self._entries)

    def __exit__(self, *exc_info):
        return None


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
    @staticmethod
    def _provenance_paths(provenance) -> tuple[str, ...]:
        assert provenance is not None
        return tuple(source.resource_id for source in provenance.sources)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path_form", ["absolute", "virtual", "default"])
    async def test_agrep_matches_sync_grep_provenance(
        self,
        home: Path,
        path_form: str,
    ) -> None:
        docs = home / "docs"
        docs.mkdir()
        matches = [docs / "one.txt", docs / "two.txt"]
        for match in matches:
            match.write_text("shared needle\n", encoding="utf-8")
        backend = WriteGuardBackend(root_dir=home, writable_dirs=["state"])
        path = {"absolute": str(docs), "virtual": "/docs", "default": None}[path_form]

        sync_token = begin_protected_result_capture()
        try:
            sync_result = backend.grep("shared needle", path)
        finally:
            sync_provenance = end_protected_result_capture(sync_token)

        async_token = begin_protected_result_capture()
        try:
            async_result = await backend.agrep("shared needle", path)
        finally:
            async_provenance = end_protected_result_capture(async_token)

        expected = {str(match.resolve()) for match in matches}
        assert sync_result.error is None
        assert async_result.error is None
        sync_paths = self._provenance_paths(sync_provenance)
        async_paths = self._provenance_paths(async_provenance)
        assert len(async_paths) == len(sync_paths) == len(expected)
        assert set(async_paths) == set(sync_paths) == expected

    @pytest.mark.asyncio
    async def test_agrep_provenance_preserves_per_file_integrity_downgrade(
        self,
        home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("MIMIR_HOME", str(home))
        docs = home / "docs"
        docs.mkdir()
        trusted = docs / "trusted.txt"
        untrusted = docs / "untrusted.txt"
        trusted.write_text("shared needle\n", encoding="utf-8")
        untrusted.write_text("shared needle\n", encoding="utf-8")
        (home / ".mimir" / "file-integrity.json").write_text(
            json.dumps({"docs/untrusted.txt": "untrusted"}),
            encoding="utf-8",
        )
        backend = WriteGuardBackend(root_dir=home, writable_dirs=["state"])

        token = begin_protected_result_capture()
        try:
            result = await backend.agrep("shared needle", str(docs))
        finally:
            provenance = end_protected_result_capture(token)

        assert result.error is None
        assert set(self._provenance_paths(provenance)) == {
            str(trusted.resolve()), str(untrusted.resolve()),
        }
        labels = classify_protected_result(
            "agrep",
            {"path": str(docs)},
            None,
            ToolAuthorization(
                tool_name="agrep",
                decision=OperationDecision.RESOURCE_SCOPED,
                allowed=True,
            ),
            provenance=provenance,
        )
        assert labels is not None
        assert labels.has_untrusted_active_ingest is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "method",
        [
            name
            for name, member in vars(_RootAwareFilesystemBackend).items()
            if inspect.iscoroutinefunction(member)
            and name.startswith("a")
            and name[1:] in vars(_RootAwareFilesystemBackend)
            and any(
                "_publish_read" in referenced_name
                for referenced_name in vars(
                    _RootAwareFilesystemBackend
                )[name[1:]].__code__.co_names
            )
        ],
    )
    async def test_every_async_read_override_publishes_exact_provenance(
        self,
        home: Path,
        method: str,
    ) -> None:
        docs = home / "docs"
        docs.mkdir()
        target = docs / "match.txt"
        target.write_text("shared needle\n", encoding="utf-8")
        backend = WriteGuardBackend(root_dir=home, writable_dirs=["state"])
        calls = {
            "aread": lambda: backend.aread(str(target)),
            "als": lambda: backend.als(str(docs)),
            "aglob": lambda: backend.aglob("*.txt", str(docs)),
            "agrep": lambda: backend.agrep("shared needle", str(docs)),
        }

        token = begin_protected_result_capture()
        try:
            result = await calls[method]()
        finally:
            provenance = end_protected_result_capture(token)

        assert result.error is None
        assert self._provenance_paths(provenance) == (str(target.resolve()),)

    @pytest.mark.parametrize("is_service", [False, True], ids=["non-admin", "service"])
    def test_skill_roots_are_readable_but_protected_names_and_writes_are_refused(
        self, home: Path, monkeypatch: pytest.MonkeyPatch, is_service: bool,
    ) -> None:
        monkeypatch.setenv("MIMIR_HOME", str(home))
        skill_files = []
        protected_files = []
        for root_name, skill_name in (
            ("skills", "operator-skill"),
            (".mimir_builtin_skills", "builtin-skill"),
        ):
            skill_dir = home / root_name / skill_name
            skill_dir.mkdir(parents=True)
            skill_file = skill_dir / "SKILL.md"
            skill_file.write_text(f"# {skill_name}\n", encoding="utf-8")
            skill_files.append(skill_file)
            for relative in (".env.local", "private.key", "certificate.pem", "credentials/note.md"):
                protected = skill_dir / relative
                protected.parent.mkdir(parents=True, exist_ok=True)
                protected.write_text("withheld\n", encoding="utf-8")
                protected_files.append(protected)

        service = ServicePrincipal(
            canonical="skills-loader",
            trigger="scheduled_tick",
            capabilities=("read_file",),
        )
        auth = AuthContext(
            principal="service:skills-loader" if is_service else "user:test",
            canonical_principal="skills-loader" if is_service else "test",
            roles=("service",) if is_service else ("user",),
            event_ingress=None,
            trigger=service.trigger if is_service else "user_message",
            channel_id="channel",
            interactivity=None,
            is_service=is_service,
            service_authority=service if is_service else None,
            enforcement_enabled=True,
        )
        backend = WriteGuardBackend(root_dir=home, writable_dirs=["state"])

        token = set_current_turn(SimpleNamespace(turn_id="skills-read", auth_context=auth))
        try:
            reads = [backend.read(str(path)) for path in skill_files]
            protected_reads = [backend.read(str(path)) for path in protected_files]
            writes = [backend.write(str(path), "replacement\n") for path in skill_files]
        finally:
            reset_current_turn(token)

        assert [result.file_data["content"] for result in reads] == [
            "# operator-skill\n",
            "# builtin-skill\n",
        ]
        assert all(
            result.error and "protected_name_match" in result.error
            for result in protected_reads
        )
        assert all(result.error and "Write blocked" in result.error for result in writes)
        assert [path.read_text(encoding="utf-8") for path in skill_files] == [
            "# operator-skill\n",
            "# builtin-skill\n",
        ]

    @pytest.mark.parametrize("is_service", [False, True], ids=["non-admin", "service"])
    def test_skills_middleware_loads_home_sources_without_errors(
        self,
        home: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        is_service: bool,
    ) -> None:
        monkeypatch.setenv("MIMIR_HOME", str(home))
        sources = [home / ".mimir_builtin_skills", home / "skills"]
        for source, skill_name in zip(sources, ("builtin-skill", "operator-skill"), strict=True):
            skill_dir = source / skill_name
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\n"
                f"name: {skill_name}\n"
                f"description: Test {skill_name}.\n"
                "---\n"
                "Instructions.\n",
                encoding="utf-8",
            )

        service = ServicePrincipal(canonical="skills-loader", trigger="scheduled_tick")
        auth = AuthContext(
            principal="service:skills-loader" if is_service else "user:test",
            canonical_principal="skills-loader" if is_service else "test",
            roles=("service",) if is_service else ("user",),
            event_ingress=None,
            trigger=service.trigger if is_service else "user_message",
            channel_id="channel",
            interactivity=None,
            is_service=is_service,
            service_authority=service if is_service else None,
            enforcement_enabled=True,
        )
        middleware = SkillsMiddleware(
            backend=WriteGuardBackend(root_dir=home, writable_dirs=["state"]),
            sources=[str(source) for source in sources],
        )

        token = set_current_turn(SimpleNamespace(turn_id="skills-load", auth_context=auth))
        try:
            update = middleware.before_agent({}, None, {})
        finally:
            reset_current_turn(token)

        assert update is not None
        assert {skill["name"] for skill in update["skills_metadata"]} == {
            "builtin-skill",
            "operator-skill",
        }
        assert "skills_load_errors" not in update
        assert not any("Skills load errors" in record.message for record in caplog.records)

    def test_non_admin_docs_are_readable_discoverable_and_read_only(
        self, home: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        docs = home / "docs"
        docs.mkdir()
        (docs / "README.md").write_text("reference docs\n", encoding="utf-8")
        protected = (
            docs / ".env.example",
            docs / ".env.local",
            docs / "private.key",
            docs / "certificate.pem",
            docs / "credentials" / "notes.md",
        )
        for path in protected:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("withheld\n", encoding="utf-8")
        monkeypatch.setenv("MIMIR_HOME", str(home))
        auth = AuthContext(
            principal="user:test",
            canonical_principal="test",
            roles=("user",),
            event_ingress=None,
            trigger="user_message",
            channel_id="channel",
            interactivity=None,
            is_service=False,
            enforcement_enabled=True,
        )
        backend = WriteGuardBackend(root_dir=home, writable_dirs=["state"])

        token = set_current_turn(SimpleNamespace(turn_id="docs-read", auth_context=auth))
        try:
            read = backend.read("/docs/README.md")
            glob = backend.glob("*.md", path="/docs")
            listing = backend.ls("/docs")
            write = backend.write("/docs/agent-note.md", "blocked\n")
            denied = [backend.read(str(path)) for path in protected]
        finally:
            reset_current_turn(token)

        assert read.error is None
        assert read.file_data["content"] == "reference docs\n"
        assert [match["path"] for match in glob.matches] == ["/docs/README.md"]
        assert [entry["path"] for entry in listing.entries] == ["/docs/README.md"]
        assert "Write blocked" in (write.error or "")
        assert not (docs / "agent-note.md").exists()
        assert all(result.error and "protected_name_match" in result.error for result in denied)

    def test_service_turn_offloads_large_result_and_reads_it_back(
        self, home: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("MIMIR_HOME", str(home))
        backend = WriteGuardBackend(root_dir=home, writable_dirs=["state"])
        middleware = MimirFilesystemMiddleware(
            backend=backend, tool_token_limit_before_evict=1,
        )
        auth = AuthContext(
            principal="service:synthesis",
            canonical_principal="synthesis",
            roles=("service",),
            event_ingress=None,
            trigger="saga_session_end",
            channel_id="channel",
            interactivity=None,
            is_service=True,
            enforcement_enabled=True,
        )
        content = "offloaded service result " * 10

        token = set_current_turn(SimpleNamespace(turn_id="spill", auth_context=auth))
        try:
            processed, evicted = middleware._process_large_message(
                ToolMessage(content=content, tool_call_id="service-call"), backend,
            )
            result = backend.read(f"{middleware._large_tool_results_prefix}/service-call")
        finally:
            reset_current_turn(token)

        artifact_root = framework_large_tool_results_root(home)
        assert artifact_root is not None
        assert evicted is True
        assert str(middleware._large_tool_results_prefix) in str(processed.content)
        assert result.error is None
        assert result.file_data["content"] == content
        assert (artifact_root / "service-call").read_text(encoding="utf-8") == content

        sibling = artifact_root.parent / f"{artifact_root.name}-private" / "blocked.txt"
        denied = backend.write(str(sibling), "blocked")
        assert "Write blocked" in (denied.error or "")

    def test_allows_write_to_writable_root(self, home: Path) -> None:
        b = WriteGuardBackend(root_dir=home, writable_dirs=["state"])
        r = b.write(file_path="/state/note.txt", content="hi")
        assert getattr(r, "error", None) is None

    def test_allows_write_to_nested_path_under_writable_root(self, home: Path) -> None:
        b = WriteGuardBackend(root_dir=home, writable_dirs=["state"])
        r = b.write(file_path="/state/sub/dir/note.txt", content="hi")
        assert getattr(r, "error", None) is None

    def test_blocks_write_to_non_writable_dir(
        self, home: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        events = []
        monkeypatch.setattr(
            "mimir.tools.budget_gate._emit_event_sync",
            lambda kind, **fields: events.append((kind, fields)),
        )
        b = WriteGuardBackend(root_dir=home, writable_dirs=["state"])
        r = b.write(file_path="/logs/bad.txt", content="hi")
        assert "Write blocked" in (getattr(r, "error", "") or "")
        hard = next(fields for kind, fields in events if kind == "hard_boundary_denied")
        assert hard["tool"] == "write_file"
        assert hard["boundary"] == "filesystem_write_guard"
        assert hard["reason"] == "filesystem_target_not_writable"
        assert hard["target"] == str(home / "logs" / "bad.txt")

    def test_blocks_write_to_implicit_dir(self, home: Path) -> None:
        # .mimir/ is not in writable_dirs; saga db must not be writable
        # via deepagents Write tool.
        b = WriteGuardBackend(root_dir=home, writable_dirs=["state"])
        r = b.write(file_path="/.mimir/db.sqlite", content="hi")
        assert "Write blocked" in (getattr(r, "error", "") or "")

    @pytest.mark.parametrize(
        "file_path",
        [
            "/pollers-overrides.yaml",
            "/saga.toml",
            "/compose.env",
        ],
    )
    def test_blocks_top_level_home_files(self, home: Path, file_path: str) -> None:
        # pollers-overrides.yaml is agent-managed through its dedicated validated
        # tool, not by widening the generic file tool's top-level write access.
        b = WriteGuardBackend(root_dir=home, writable_dirs=["state"])
        r = b.write(file_path=file_path, content="x\n")
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

    @pytest.mark.parametrize(
        "file_path",
        ["attachments/fetch-cache/body.txt", "/attachments/fetch-cache/body.txt"],
    )
    def test_read_fetch_cache_relative_and_virtual_paths(
        self, home: Path, file_path: str,
    ) -> None:
        cache = home / "attachments" / "fetch-cache"
        cache.mkdir(parents=True)
        (cache / "body.txt").write_text("fetched body\n", encoding="utf-8")
        backend = WriteGuardBackend(root_dir=home, writable_dirs=["state"])

        result = backend.read(file_path=file_path)

        assert result.error is None
        assert result.file_data["content"] == "fetched body\n"

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

    def test_blocks_internal_symlink(self, home: Path) -> None:
        (home / "state" / "sub").mkdir()
        link = home / "state" / "alias"
        link.symlink_to(home / "state" / "sub", target_is_directory=True)
        b = WriteGuardBackend(root_dir=home, writable_dirs=["state"])
        r = b.write(file_path="/state/alias/note.txt", content="ok")
        assert r.error == (
            "Write blocked: path contains a symbolic-link or non-directory component."
        )
        assert not (home / "state" / "sub" / "note.txt").exists()

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
        # Only ``state`` and the framework-owned artifact root should survive.
        assert b._writable_roots == [
            (home / "state").resolve(), framework_large_tool_results_root(home),
        ]
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
        assert callable(b.ls)

    def test_current_async_fs_methods_forward(self, home: Path) -> None:
        b = WriteGuardBackend(root_dir=home, writable_dirs=["state"])
        assert callable(b.ls)
        assert callable(b.grep)
        assert callable(b.glob)
        assert callable(b.als)
        assert callable(b.agrep)
        assert callable(b.aglob)
        for obsolete in (
            "ls_info", "als_info", "glob_info", "aglob_info", "grep_raw", "agrep_raw",
        ):
            with pytest.raises(AttributeError):
                getattr(b, obsolete)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["aedit", "aupload_files"])
    async def test_async_mutations_run_filesystem_work_off_loop(
        self, home: Path, monkeypatch: pytest.MonkeyPatch, method: str,
    ) -> None:
        backend = WriteGuardBackend(root_dir=home, writable_dirs=["state"])
        loop_thread = threading.get_ident()
        worker_threads: list[int] = []

        if method == "aedit":
            monkeypatch.setattr(
                backend,
                "edit",
                lambda **_kwargs: worker_threads.append(threading.get_ident()),
            )
            await backend.aedit("/state/file", "old", "new")
        else:
            monkeypatch.setattr(
                backend,
                "upload_files",
                lambda _files: worker_threads.append(threading.get_ident()),
            )
            await backend.aupload_files([("/state/file", b"content")])

        assert len(worker_threads) == 1
        assert worker_threads[0] != loop_thread


class TestCreateOnlyWrites:
    def test_middleware_dispatches_only_approved_filesystem_tools(
        self, home: Path,
    ) -> None:
        middleware = MimirFilesystemMiddleware(
            backend=WriteGuardBackend(home, ["state"]),
            tools=["read_file", "delete"],
            custom_tool_descriptions={"write_file": "overwrite anything"},
        )

        assert [tool.name for tool in middleware.tools] == [
            "ls", "read_file", "write_file", "edit_file", "glob", "grep", "execute",
        ]
        write_tool = next(tool for tool in middleware.tools if tool.name == "write_file")
        assert write_tool.description == (
            "Creates a new file and writes the supplied content. It never overwrites an "
            "existing path; when the target exists, use `edit_file` instead. Parent "
            "directories are created as needed."
        )

    def test_home_collision_uses_canonical_virtual_path_without_audit(
        self, home: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from mimir.models import TurnContext

        target = home / "state" / "existing.txt"
        target.write_text("original", encoding="utf-8")
        events = []
        monkeypatch.setattr(
            "mimir.tools.budget_gate._emit_event_sync",
            lambda kind, **fields: events.append((kind, fields)),
        )
        backend = WriteGuardBackend(home, ["state"])

        token = set_current_turn(TurnContext(
            turn_id="collision-turn",
            session_id="session",
            trigger="user_message",
            channel_id="discord-channel",
            started_at=0.0,
        ))
        try:
            result = backend.write(str(target), "replacement")
        finally:
            reset_current_turn(token)

        assert result.error == (
            "File '/state/existing.txt' already exists. Use edit_file to modify existing files."
        )
        assert target.read_text(encoding="utf-8") == "original"
        assert backend.drain_denials() == []
        assert events == []

    @pytest.mark.asyncio
    async def test_home_async_collision_uses_canonical_virtual_path(self, home: Path) -> None:
        target = home / "state" / "existing-async.txt"
        target.write_text("original", encoding="utf-8")
        backend = WriteGuardBackend(home, ["state"])

        result = await backend.awrite(str(target), "replacement")

        assert result.error == (
            "File '/state/existing-async.txt' already exists. Use edit_file to modify "
            "existing files."
        )
        assert target.read_text(encoding="utf-8") == "original"
        assert backend.drain_denials() == []

    @pytest.mark.asyncio
    async def test_routed_sync_and_async_collisions_name_original_requests(
        self, tmp_path: Path,
    ) -> None:
        home = _split_home(tmp_path)
        route = tmp_path / "route"
        route.mkdir()
        (route / "sync.txt").write_text("sync", encoding="utf-8")
        (route / "async.txt").write_text("async", encoding="utf-8")
        router = FileToolRouter(
            default=WriteGuardBackend(home, ["state"]),
            routes=build_file_tool_routes([(str(route), "rw")]),
        )
        sync_path = str(route / "sync.txt")
        async_path = str(route / "async.txt")

        sync_result = router.write(sync_path, "changed")
        async_result = await router.awrite(async_path, "changed")

        assert sync_result.error == (
            f"File '{sync_path}' already exists. Use edit_file to modify existing files."
        )
        assert async_result.error == (
            f"File '{async_path}' already exists. Use edit_file to modify existing files."
        )
        assert sync_result.error != (
            "File '/sync.txt' already exists. Use edit_file to modify existing files."
        )
        assert async_result.error != (
            "File '/async.txt' already exists. Use edit_file to modify existing files."
        )
        assert (route / "sync.txt").read_text(encoding="utf-8") == "sync"
        assert (route / "async.txt").read_text(encoding="utf-8") == "async"

    def test_home_write_failure_uses_canonical_virtual_path(self, home: Path) -> None:
        target = home / "state" / "invalid-utf8.txt"
        backend = WriteGuardBackend(home, ["state"])

        result = backend.write(str(target), "\ud800")

        assert result.error is not None
        assert result.error.startswith("Error writing file '/state/invalid-utf8.txt': ")
        assert "surrogates not allowed" in result.error
        assert f"'{target}'" not in result.error
        assert type(result.error) is str
        assert not target.exists()  # a failed create must not consume the path

    def test_failed_create_leaves_path_free_for_a_corrected_retry(
        self, home: Path,
    ) -> None:
        """A failed create-only write must not turn into a permanent collision.

        ``_exclusive_write`` opens with ``O_CREAT | O_EXCL``, so the destination
        exists before the content is written. If the write then fails and the
        file is left behind, the corrected retry hits FileExistsError and is
        told to use ``edit_file`` on content that was never written -- a
        transient encoding error becoming permanent.
        """
        target = home / "state" / "retry-after-failure.txt"
        backend = WriteGuardBackend(home, ["state"])

        failed = backend.write(str(target), "\ud800")
        assert failed.error is not None
        assert not target.exists()

        retried = backend.write(str(target), "corrected content")

        assert not getattr(retried, "error", None), retried
        assert target.read_text(encoding="utf-8") == "corrected content"

    @pytest.mark.asyncio
    async def test_routed_sync_and_async_write_failures_name_original_requests(
        self, tmp_path: Path,
    ) -> None:
        home = _split_home(tmp_path)
        route = tmp_path / "route"
        route.mkdir()
        router = FileToolRouter(
            default=WriteGuardBackend(home, ["state"]),
            routes=build_file_tool_routes([(str(route), "rw")]),
        )
        sync_path = str(route / "sync-invalid-utf8.txt")
        async_path = str(route / "async-invalid-utf8.txt")

        sync_result = router.write(sync_path, "\ud800")
        async_result = await router.awrite(async_path, "\ud800")

        for result, request, stripped in (
            (sync_result, sync_path, "/sync-invalid-utf8.txt"),
            (async_result, async_path, "/async-invalid-utf8.txt"),
        ):
            assert result.error is not None
            assert result.error.startswith(f"Error writing file '{request}': ")
            assert "surrogates not allowed" in result.error
            assert f"'{stripped}'" not in result.error
            assert type(result.error) is str
        assert not Path(sync_path).exists()  # failed creates leave no destination
        assert not Path(async_path).exists()

    @pytest.mark.parametrize("depth", [0, 1, 2])
    def test_intermediate_symlink_at_every_depth_is_refused(
        self, tmp_path: Path, depth: int,
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        components = ["one", "two", "three"]
        parent = root
        for component in components[:depth]:
            parent = parent / component
            parent.mkdir()
        decoy = outside / f"decoy-{depth}"
        decoy.mkdir()
        remaining = decoy
        for component in components[depth + 1:]:
            remaining = remaining / component
            remaining.mkdir()
        (parent / components[depth]).symlink_to(decoy, target_is_directory=True)
        backend = _RootAwareFilesystemBackend(root_dir=root, virtual_mode=True)

        result = backend.write("/one/two/three/result.txt", "blocked")

        assert result.error == (
            "Write blocked: path contains a symbolic-link or non-directory component."
        )
        assert not (remaining / "result.txt").exists()

    @pytest.mark.parametrize("kind", ["file", "directory", "dangling"])
    def test_final_symlink_is_refused_without_changing_target(
        self, tmp_path: Path, kind: str,
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        target = outside / "target"
        if kind == "file":
            target.write_text("original", encoding="utf-8")
        elif kind == "directory":
            target.mkdir()
        link = root / "link"
        link.symlink_to(target, target_is_directory=kind == "directory")
        backend = _RootAwareFilesystemBackend(root_dir=root, virtual_mode=True)

        result = backend.write("/link", "blocked")

        assert result.error == (
            "Write blocked: path contains a symbolic-link or non-directory component."
        )
        if kind == "file":
            assert target.read_text(encoding="utf-8") == "original"
        elif kind == "directory":
            assert list(target.iterdir()) == []
        else:
            assert not target.exists()

    def test_non_directory_intermediate_component_is_refused(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        (root / "component").write_text("original", encoding="utf-8")
        backend = _RootAwareFilesystemBackend(root_dir=root, virtual_mode=True)

        result = backend.write("/component/result.txt", "blocked")

        assert result.error == (
            "Write blocked: path contains a symbolic-link or non-directory component."
        )
        assert (root / "component").read_text(encoding="utf-8") == "original"

    def test_swap_before_component_open_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "root"
        (root / "one").mkdir(parents=True)
        outside = tmp_path / "outside"
        (outside / "two").mkdir(parents=True)
        backend = _RootAwareFilesystemBackend(root_dir=root, virtual_mode=True)
        real_open = os.open
        swapped = False

        def swapping_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal swapped
            if path == "one" and dir_fd is not None and not swapped:
                swapped = True
                (root / "one").rename(root / "original-one")
                (root / "one").symlink_to(outside, target_is_directory=True)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr("mimir.readonly_backend.os.open", swapping_open)

        result = backend.write("/one/two/result.txt", "blocked")

        assert result.error == (
            "Write blocked: path contains a symbolic-link or non-directory component."
        )
        assert not (outside / "two" / "result.txt").exists()

    def test_swap_after_component_open_cannot_redirect_later_operations(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "root"
        (root / "one").mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        backend = _RootAwareFilesystemBackend(root_dir=root, virtual_mode=True)
        real_open = os.open
        swapped = False

        def swapping_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal swapped
            descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
            if path == "one" and dir_fd is not None and not swapped:
                swapped = True
                (root / "one").rename(root / "opened-one")
                (root / "one").symlink_to(outside, target_is_directory=True)
            return descriptor

        monkeypatch.setattr("mimir.readonly_backend.os.open", swapping_open)

        result = backend.write("/one/two/result.txt", "created")

        assert result.error is None
        assert (root / "opened-one" / "two" / "result.txt").read_text() == "created"
        assert not (outside / "two").exists()

    def test_descriptor_walk_uses_required_flags_and_modes(
        self, home: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        backend = WriteGuardBackend(home, ["state"])
        real_open = os.open
        real_mkdir = os.mkdir
        open_calls = []
        mkdir_calls = []

        def recording_open(path, flags, mode=0o777, *, dir_fd=None):
            open_calls.append((path, flags, mode, dir_fd))
            return real_open(path, flags, mode, dir_fd=dir_fd)

        def recording_mkdir(path, mode=0o777, *, dir_fd=None):
            mkdir_calls.append((path, mode, dir_fd))
            return real_mkdir(path, mode, dir_fd=dir_fd)

        monkeypatch.setattr("mimir.readonly_backend.os.open", recording_open)
        monkeypatch.setattr("mimir.readonly_backend.os.mkdir", recording_mkdir)

        result = backend.write("/state/alpha/beta/result.txt", "line\n")

        assert result.error is None
        assert open_calls[0][1] == os.O_RDONLY | os.O_DIRECTORY
        intermediate = open_calls[1:-1]
        assert [call[0] for call in intermediate] == ["state", "alpha", "beta"]
        assert all(
            call[1] == os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            for call in intermediate
        )
        final = open_calls[-1]
        assert final[0] == "result.txt"
        assert final[1] == os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        assert final[1] & os.O_TRUNC == 0
        assert final[2] == 0o644
        assert [call[1] for call in mkdir_calls] == [0o777, 0o777, 0o777]

    def test_missing_descriptor_primitives_fail_closed(
        self, home: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd - {os.open})
        backend = WriteGuardBackend(home, ["state"])

        result = backend.write("/state/result.txt", "blocked")

        assert result.error == (
            "Write failed: descriptor-relative file creation is unsupported on this platform."
        )
        assert not (home / "state" / "result.txt").exists()
        assert backend.drain_denials() == []

    def test_concurrent_creators_have_one_success_and_exact_collisions(
        self, home: Path,
    ) -> None:
        backend = WriteGuardBackend(home, ["state"])
        expected = (
            "File '/state/race.txt' already exists. Use edit_file to modify existing files."
        )

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(
                lambda value: backend.write("/state/race.txt", value),
                [f"creator-{index}" for index in range(8)],
            ))

        assert sum(result.error is None for result in results) == 1
        assert [result.error for result in results].count(expected) == 7
        assert (home / "state" / "race.txt").read_text().startswith("creator-")
        assert backend.drain_denials() == []

    @pytest.mark.parametrize(
        ("file_path", "writable_dirs", "expected_op", "expected_reason"),
        [
            ("/logs/blocked.txt", ["state"], "write", "filesystem_target_not_writable"),
            ("/prompts/blocked.txt", ["state"], "write_prompts_readonly", "prompts_readonly"),
            ("/memory/core/blocked.txt", ["memory"], "write_core_memory_readonly", "core_memory_readonly"),
            ("/state/identities.yaml", ["state"], "write_identities_protected", "identities_protected"),
            ("/state/../logs/blocked.txt", ["state"], "write", "filesystem_target_not_writable"),
        ],
    )
    def test_authorization_denial_audit_and_turn_precede_creation(
        self,
        home: Path,
        monkeypatch: pytest.MonkeyPatch,
        file_path: str,
        writable_dirs: list[str],
        expected_op: str,
        expected_reason: str,
    ) -> None:
        from mimir.models import TurnContext

        (home / "memory" / "core").mkdir()
        events = []
        monkeypatch.setattr(
            "mimir.tools.budget_gate._emit_event_sync",
            lambda kind, **fields: events.append((kind, fields)),
        )
        backend = WriteGuardBackend(home, writable_dirs)
        token = set_current_turn(TurnContext(
            turn_id="authorization-turn",
            session_id="session",
            trigger="user_message",
            channel_id="discord-channel",
            started_at=0.0,
        ))
        try:
            result = backend.write(file_path, "blocked")
        finally:
            reset_current_turn(token)

        assert result.error
        denial = backend.drain_denials()
        assert [(entry["op"], entry["turn_id"]) for entry in denial] == [
            (expected_op, "authorization-turn"),
        ]
        hard = next(fields for kind, fields in events if kind == "hard_boundary_denied")
        assert hard["reason"] == expected_reason

    def test_empty_rendering_and_read_pagination_contract(self, home: Path) -> None:
        (home / "page.txt").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
        middleware = MimirFilesystemMiddleware(backend=WriteGuardBackend(home, ["state"]))
        tools = {tool.name: tool for tool in middleware.tools}
        runtime = SimpleNamespace(tool_call_id="filesystem-rendering")

        ls_result = tools["ls"].func(path="/state", runtime=runtime)
        glob_result = tools["glob"].func(pattern="*.missing", path="/", runtime=runtime)
        read_result = tools["read_file"].func(
            file_path="/page.txt", offset=1, limit=2, runtime=runtime,
        )
        zero_result = tools["read_file"].func(
            file_path="/page.txt", offset=0, limit=0, runtime=runtime,
        )

        assert ls_result.content == "No files found"
        assert glob_result.content == "No files found"
        assert read_result.content == (
            "2  two\n3  three\n\n[Read 2 lines (lines 2-3 of 4 total). "
            "1 line remaining from offset 3.]"
        )
        assert "no lines were read because `limit` was 0" in zero_result.content


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

    def test_blocks_untrusted_poller_write_to_core_memory(
        self, home_with_memory: Path
    ) -> None:
        b = WriteGuardBackend(root_dir=home_with_memory, writable_dirs=["memory"])
        ctx = self._make_turn_ctx(trigger="poller", channel_id="poller:external-feed")
        tok = self._set_turn(ctx)
        try:
            result = b.write(
                file_path="/memory/core/00-persona.md",
                content="untrusted instruction",
            )
            assert "read-only" in (getattr(result, "error", "") or "")
        finally:
            self._clear_turn(tok)

    @pytest.mark.parametrize(
        ("trigger", "channel_id"),
        [
            ("scheduled_tick", "scheduler:reflect"),
            ("saga_session_end", "discord-a"),
        ],
    )
    def test_blocks_core_memory_write_in_reflection_turn(
        self, home_with_memory: Path, trigger: str, channel_id: str,
    ) -> None:
        """Reflection and session synthesis may read core, but never write it."""
        b = WriteGuardBackend(root_dir=home_with_memory, writable_dirs=["memory"])
        ctx = self._make_turn_ctx(trigger=trigger, channel_id=channel_id)
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
    def test_blocks_all_writes(
        self, home: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        events = []
        monkeypatch.setattr(
            "mimir.tools.budget_gate._emit_event_sync",
            lambda kind, **fields: events.append((kind, fields)),
        )
        b = ReadOnlyFilesystemBackend(root_dir=home)
        r = b.write(file_path="/state/anywhere.txt", content="no")
        assert "read-only" in (getattr(r, "error", "") or "")
        hard = next(fields for kind, fields in events if kind == "hard_boundary_denied")
        assert hard["tool"] == "write_file"
        assert hard["boundary"] == "readonly_filesystem"
        assert hard["reason"] == "write_readonly"
        assert hard["target"] == str(home / "state" / "anywhere.txt")

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

    def test_guard_on_ls_root_lists_home_entries(self, tmp_path: Path) -> None:
        home = _split_home(tmp_path)
        b = WriteGuardBackend(root_dir=home, writable_dirs=["state"], guard_outside_root=True)
        r = b.ls(path="/")
        names = {Path(e["path"].rstrip("/")).name for e in (r.entries or [])}
        assert r.error is None
        assert {"state", "logs"} <= names

    def test_guard_on_allows_virtual_path_that_collides_with_real_root_dir(
        self, tmp_path: Path,
    ) -> None:
        home = _split_home(tmp_path)
        (home / "var").mkdir()
        (home / "var" / "home-file.txt").write_text("home var\n")
        b = WriteGuardBackend(root_dir=home, writable_dirs=["state"], guard_outside_root=True)
        r = b.ls(path="/var")
        names = {Path(e["path"].rstrip("/")).name for e in (r.entries or [])}
        assert r.error is None
        assert "home-file.txt" in names

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
        assert routes[str(rw) + "/"].virtual_mode is True
        assert routes[str(ro) + "/"]._fs.virtual_mode is True

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

    def test_grep_returns_bounded_overlapping_context_once(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "sample.txt").write_text(
            "one\nneedle first\nbetween\nneedle second\nfive\n",
            encoding="utf-8",
        )
        backend = _RootAwareFilesystemBackend(root_dir=repo, virtual_mode=True)

        result = backend.grep(
            "needle", path="/sample.txt", before_context=1, after_context=1,
        )

        assert result.error is None
        assert [(m["line"], m["text"]) for m in result.matches or []] == [
            (1, "one"),
            (2, "needle first"),
            (3, "between"),
            (4, "needle second"),
            (5, "five"),
        ]

    @pytest.mark.parametrize("value", [-1, MAX_GREP_CONTEXT_LINES + 1, True])
    def test_grep_rejects_context_outside_explicit_cap(
        self, tmp_path: Path, value: object,
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "sample.txt").write_text("needle\n", encoding="utf-8")
        backend = _RootAwareFilesystemBackend(root_dir=repo, virtual_mode=True)

        result = backend.grep("needle", path="/", after_context=value)  # type: ignore[arg-type]

        assert result.matches == []
        assert f"between 0 and {MAX_GREP_CONTEXT_LINES}" in (result.error or "")

    def test_grep_tool_schema_exposes_capped_context(self, tmp_path: Path) -> None:
        (tmp_path / "sample.txt").write_text(
            "before\nneedle\nafter\n", encoding="utf-8",
        )
        backend = WriteGuardBackend(root_dir=tmp_path, writable_dirs=["state"])
        middleware = MimirFilesystemMiddleware(backend=backend)
        grep_tool = next(tool for tool in middleware.tools if tool.name == "grep")

        assert grep_tool.args["before_context"]["maximum"] == MAX_GREP_CONTEXT_LINES
        assert grep_tool.args["after_context"]["maximum"] == MAX_GREP_CONTEXT_LINES
        result = grep_tool.func(
            pattern="needle",
            runtime=SimpleNamespace(tool_call_id="grep-context"),
            path="/sample.txt",
            output_mode="content",
            before_context=1,
            after_context=1,
        )
        assert "1: before" in result.content
        assert "2: needle" in result.content
        assert "3: after" in result.content

    def test_grep_tool_declares_every_injected_arg_the_stock_tool_declares(
        self, tmp_path: Path,
    ) -> None:
        """Our wrapper must not lose an injected argument the stock tool has.

        LangChain decides this in ``StructuredTool._injected_args_keys``, which
        reads the raw ``inspect.signature`` annotation without resolving it
        through ``get_type_hints``. Because ``mimir.readonly_backend`` uses
        ``from __future__ import annotations`` while deepagents' filesystem
        module does not, our ``runtime`` annotation is a plain string and was
        not recognised as injected -- which silently dropped it during input
        validation. Compare against the stock tool rather than hard-coding
        ``{"runtime"}`` so a new injected parameter upstream also trips this.
        """
        backend = WriteGuardBackend(root_dir=tmp_path, writable_dirs=["state"])
        stock_tool = FilesystemMiddleware(backend=backend)._create_grep_tool()
        our_tool = MimirFilesystemMiddleware(backend=backend)._create_grep_tool()

        assert stock_tool._injected_args_keys, "stock tool declares no injected args"
        missing = stock_tool._injected_args_keys - our_tool._injected_args_keys
        assert not missing, f"wrapper dropped injected args: {sorted(missing)}"

    def test_grep_tool_keeps_injected_runtime_through_input_validation(
        self, tmp_path: Path,
    ) -> None:
        """Invoke the way LangGraph does, so validation is exercised.

        ``ToolNode`` puts the resolved runtime into the tool-call arguments and
        then invokes the tool, so the value has to survive
        ``BaseTool._parse_input``. Calling ``grep_tool.func`` directly (as the
        schema test above does) hands ``runtime`` over by hand and therefore
        cannot see it being discarded.
        """
        (tmp_path / "sample.txt").write_text("before\nneedle\nafter\n", encoding="utf-8")
        backend = WriteGuardBackend(root_dir=tmp_path, writable_dirs=["state"])
        middleware = MimirFilesystemMiddleware(backend=backend)
        grep_tool = next(tool for tool in middleware.tools if tool.name == "grep")

        result = grep_tool.invoke({
            "pattern": "needle",
            "path": "/sample.txt",
            "output_mode": "content",
            "before_context": 1,
            "after_context": 1,
            # Mirrors ToolNode._inject_tool_args putting the runtime in the args.
            "runtime": SimpleNamespace(tool_call_id="grep-injected"),
        })

        assert "2: needle" in result.content
        assert result.status == "success"

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

    def test_glob_tool_omitted_path_searches_root_without_exception(
        self, tmp_path: Path,
    ) -> None:
        """DeepAgents forwards an omitted optional glob path as ``None``."""
        (tmp_path / "match.py").write_text("x\n", encoding="utf-8")
        backend = WriteGuardBackend(root_dir=tmp_path, writable_dirs=["state"])
        middleware = MimirFilesystemMiddleware(backend=backend)
        glob_tool = next(tool for tool in middleware.tools if tool.name == "glob")

        result = glob_tool.func(
            pattern="*.py",
            runtime=SimpleNamespace(tool_call_id="glob-default-root"),
        )

        assert result.status == "success"
        assert result.content == "['/match.py']"

    @pytest.mark.parametrize(
        ("kwargs", "argument", "retry_text"),
        [
            ({"pattern": None}, "pattern", "glob pattern"),
            ({"pattern": "*.py", "path": 42}, "path", "omit 'path'"),
        ],
    )
    def test_glob_invalid_arguments_return_actionable_errors(
        self,
        tmp_path: Path,
        kwargs: dict[str, object],
        argument: str,
        retry_text: str,
    ) -> None:
        backend = _RootAwareFilesystemBackend(root_dir=tmp_path, virtual_mode=True)

        result = backend.glob(**kwargs)  # type: ignore[arg-type]

        assert result.matches == []
        assert f"argument '{argument}'" in (result.error or "")
        assert retry_text in (result.error or "")

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

    def test_bounded_glob_truncation_is_deterministic(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        for name in ("b.py", "a.py"):
            (repo / "src" / name).write_text("x\n")
        backend = _RootAwareFilesystemBackend(
            root_dir=repo,
            virtual_mode=True,
            max_glob_matches=1,
        )
        original_scandir = os.scandir

        def reverse_scandir(path):
            entries = list(original_scandir(path))
            entries.reverse()
            return _ScandirEntries(entries)

        monkeypatch.setattr(os, "scandir", reverse_scandir)

        result = backend.glob("**/*.py", path="/")

        assert [match["path"] for match in result.matches or []] == ["/src/a.py"]

    def test_default_glob_timeout_tracks_half_the_upstream_budget(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "a.py").write_text("x\n")
        deadlines = []
        backend = _RootAwareFilesystemBackend(root_dir=repo, virtual_mode=True)
        original_walk = backend._walk_files

        def record_deadline(root, **kwargs):
            deadlines.append(kwargs["deadline"])
            return original_walk(root, **kwargs)

        monkeypatch.setattr(backend, "_walk_files", record_deadline)
        monkeypatch.setattr("mimir.readonly_backend.deepagents_filesystem.GLOB_TIMEOUT", 8.0)
        started = time.monotonic()

        result = backend.glob("**/*.py", path="/")

        assert result.error is None
        assert deadlines[0] == pytest.approx(started + 4.0, abs=0.1)

    def test_glob_records_completed_search(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = tmp_path / "repo"
        (repo / "src" / "pkg").mkdir(parents=True)
        (repo / "src" / "pkg" / "app.py").write_text("x\n")
        (repo / "docs" / "large").mkdir(parents=True)
        for index in range(20):
            (repo / "docs" / "large" / f"ignored-{index}.py").write_text("x\n")
        events = []
        monkeypatch.setattr(
            "mimir.event_logger.log_event_sync",
            lambda kind, **fields: events.append((kind, fields)),
        )
        backend = _RootAwareFilesystemBackend(root_dir=repo, virtual_mode=True)

        result = backend.glob("src/**/*.py", path="/")

        assert [match["path"] for match in result.matches or []] == ["/src/pkg/app.py"]
        assert result.truncated is False
        assert events == [(
            "filesystem_glob_search",
            {
                "pattern": "src/**/*.py",
                "root": str(repo),
                "visited_entries": 25,
                "elapsed_seconds": events[0][1]["elapsed_seconds"],
                "truncated": False,
                "reason": None,
            },
        )]

    def test_glob_prunes_directory_only_pattern_without_hiding_file_matches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = tmp_path / "repo"
        (repo / "one" / "nested").mkdir(parents=True)
        (repo / "one" / "nested" / "file.txt").write_text("x\n")
        (repo / "two" / "nested").mkdir(parents=True)
        (repo / "two" / "nested" / "file.txt").write_text("x\n")
        events = []
        monkeypatch.setattr(
            "mimir.event_logger.log_event_sync",
            lambda kind, **fields: events.append((kind, fields)),
        )
        backend = _RootAwareFilesystemBackend(root_dir=repo, virtual_mode=True)

        result = backend.glob("nested/", path="/")

        assert result.matches == []
        assert result.truncated is False
        assert events[0][1]["visited_entries"] == 2

    def test_large_glob_returns_time_truncation_before_external_deadline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog,
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        for directory_index in range(40):
            directory = repo / f"directory-{directory_index:02d}"
            directory.mkdir()
            for file_index in range(100):
                (directory / f"file-{file_index:03d}.txt").touch()
        events = []
        monkeypatch.setattr(
            "mimir.event_logger.log_event_sync",
            lambda kind, **fields: events.append((kind, fields)),
        )
        backend = _RootAwareFilesystemBackend(
            root_dir=repo,
            virtual_mode=True,
            glob_timeout_seconds=0.001,
            max_scan_files=100_000,
        )

        started = time.monotonic()
        result = backend.glob("**/*.py", path="/")
        elapsed = time.monotonic() - started

        assert elapsed < 1.0
        assert result.error is None
        assert result.truncated is True
        assert str(repo) in caplog.text
        assert "was cut short" in caplog.text
        kind, fields = events[-1]
        assert kind == "filesystem_glob_search"
        assert fields["pattern"] == "**/*.py"
        assert fields["root"] == str(repo)
        assert fields["visited_entries"] > 0
        assert fields["truncated"] is True
        assert fields["reason"] == "ran longer than 0.001s"

    def test_glob_time_truncation_always_reports_forward_progress(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A time-truncated walk must have visited at least one entry.

        The deadline used to be checked before the first visit, so a budget
        that expired before the walk began reported ``truncated`` with
        ``visited_entries=0`` — a result that says nothing about how far the
        scan reached, and a bound that raced process scheduling instead of the
        size of the tree. ``test_large_glob_returns_time_truncation_before_
        external_deadline`` failed roughly 1 run in 6 under CPU contention for
        exactly this reason, and consumed a worklink build attempt.

        The budget here is already spent on entry, which is the worst case and
        needs no timing to reproduce.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        for directory_index in range(4):
            directory = repo / f"directory-{directory_index}"
            directory.mkdir()
            for file_index in range(50):
                (directory / f"file-{file_index:03d}.txt").touch()
        events = []
        monkeypatch.setattr(
            "mimir.event_logger.log_event_sync",
            lambda kind, **fields: events.append((kind, fields)),
        )
        backend = _RootAwareFilesystemBackend(
            root_dir=repo,
            virtual_mode=True,
            glob_timeout_seconds=0.0,
            max_scan_files=100_000,
        )

        result = backend.glob("**/*.py", path="/")

        assert result.error is None
        assert result.truncated is True
        kind, fields = events[-1]
        assert kind == "filesystem_glob_search"
        assert fields["truncated"] is True
        assert fields["visited_entries"] >= 1, (
            "a truncated walk reported visiting nothing; the deadline fired "
            "before any forward progress"
        )

    def test_glob_visited_count_has_no_common_case_regression(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        for directory_index in range(20):
            directory = repo / f"directory-{directory_index:02d}"
            directory.mkdir()
            for file_index in range(100):
                (directory / f"file-{file_index:03d}.txt").touch()
        events = []
        monkeypatch.setattr(
            "mimir.event_logger.log_event_sync",
            lambda kind, **fields: events.append((kind, fields)),
        )
        backend = _RootAwareFilesystemBackend(
            root_dir=repo,
            virtual_mode=True,
            max_scan_files=100_000,
        )

        started = time.perf_counter()
        result = backend.glob("**/*.py", path="/")
        elapsed = time.perf_counter() - started

        assert result.error is None
        assert result.truncated is False
        assert events[-1][1]["visited_entries"] == 2_020
        assert elapsed < 1.0

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

    def test_denied_named_target_errors_for_ls_and_glob_without_disclosing_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        home = tmp_path / "home"
        (home / "logs").mkdir(parents=True)
        (home / "logs" / "withheld-name.md").write_text("private\n", encoding="utf-8")
        (home / "state").mkdir()
        monkeypatch.setenv("MIMIR_HOME", str(home))
        auth = AuthContext(
            principal="u", canonical_principal="u", roles=("user",),
            event_ingress=None, trigger="user_message", channel_id="c",
            interactivity=None, enforcement_enabled=True,
        )
        token = set_current_turn(SimpleNamespace(turn_id="named-denial", auth_context=auth))
        try:
            backend = _RootAwareFilesystemBackend(root_dir=home, virtual_mode=True)
            ls_result = backend.ls("/logs")
            glob_result = backend.glob("*.md", path="/logs")
        finally:
            reset_current_turn(token)

        for error in (ls_result.error, glob_result.error):
            assert error == (
                "Read denied: mimir_home_read_boundary. Use an allowed state path instead."
            )
            assert "logs" not in error
            assert "withheld-name" not in error
        assert glob_result.matches == []

    def test_allowed_root_returns_partial_results_with_scope_denial_notices(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        home = tmp_path / "home"
        (home / "state").mkdir(parents=True)
        (home / "state" / "visible.txt").write_text("visible\n", encoding="utf-8")
        (home / "withheld-subtree").mkdir()
        (home / "withheld-subtree" / "hidden.txt").write_text("hidden\n", encoding="utf-8")
        monkeypatch.setenv("MIMIR_HOME", str(home))
        auth = AuthContext(
            principal="u", canonical_principal="u", roles=("user",),
            event_ingress=None, trigger="user_message", channel_id="c",
            interactivity=None, enforcement_enabled=True,
        )
        token = set_current_turn(SimpleNamespace(turn_id="partial-denial", auth_context=auth))
        try:
            backend = _RootAwareFilesystemBackend(root_dir=home, virtual_mode=True)
            glob_result = backend.glob("**/*.txt", path="/")
            middleware = MimirFilesystemMiddleware(backend=backend)
            tools = {tool.name: tool for tool in middleware.tools}
            ls_message = tools["ls"].func(
                path="/", runtime=SimpleNamespace(tool_call_id="partial-ls"),
            )
            glob_message = tools["glob"].func(
                pattern="**/*.txt", path="/",
                runtime=SimpleNamespace(tool_call_id="partial-glob"),
            )
        finally:
            reset_current_turn(token)

        assert [match["path"] for match in glob_result.matches or []] == [
            "/state/visible.txt",
        ]
        assert glob_result.error is None
        assert glob_result.truncated is False
        for message in (ls_message, glob_message):
            assert message.status == "success"
            assert "1 entry (mimir_home_read_boundary)" in str(message.content)
            assert "hit its time limit" not in str(message.content)
            assert "Narrow the search" not in str(message.content)
            assert "withheld-subtree" not in str(message.content)
            assert "hidden.txt" not in str(message.content)
        assert "/state/" in str(ls_message.content)
        assert "/state/visible.txt" in str(glob_message.content)

    def test_protected_name_partial_withhold_does_not_advertise_presence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        (root / "safe.txt").write_text("safe\n", encoding="utf-8")
        (root / "credentials.json").write_text("{}\n", encoding="utf-8")
        monkeypatch.setenv("MIMIR_HOME", str(tmp_path / "home"))
        auth = AuthContext(
            principal="u", canonical_principal="u", roles=("user",),
            event_ingress=None, trigger="user_message", channel_id="c",
            interactivity=None, enforcement_enabled=True,
        )
        token = set_current_turn(SimpleNamespace(turn_id="protected-name", auth_context=auth))
        try:
            backend = _RootAwareFilesystemBackend(root_dir=root, virtual_mode=True)
            middleware = MimirFilesystemMiddleware(backend=backend)
            tools = {tool.name: tool for tool in middleware.tools}
            ls_message = tools["ls"].func(
                path="/", runtime=SimpleNamespace(tool_call_id="protected-ls"),
            )
            glob_message = tools["glob"].func(
                pattern="*.txt", path="/",
                runtime=SimpleNamespace(tool_call_id="protected-glob"),
            )
        finally:
            reset_current_turn(token)

        for message in (ls_message, glob_message):
            content = str(message.content)
            assert message.status == "success"
            assert "protected_name_match" not in content
            assert "read policy withheld" not in content
            assert "credentials.json" not in content

    def test_admin_ls_and_glob_have_no_withheld_signal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        home = tmp_path / "home"
        (home / "docs").mkdir(parents=True)
        (home / "docs" / "visible.md").write_text("visible\n", encoding="utf-8")
        monkeypatch.setenv("MIMIR_HOME", str(home))
        auth = AuthContext(
            principal="admin", canonical_principal="admin", roles=("admin",),
            event_ingress=None, trigger="user_message", channel_id="c",
            interactivity=None, enforcement_enabled=True,
        )
        token = set_current_turn(SimpleNamespace(turn_id="admin-read", auth_context=auth))
        try:
            backend = _RootAwareFilesystemBackend(root_dir=home, virtual_mode=True)
            ls_result = backend.ls("/docs")
            glob_result = backend.glob("**/*.md", path="/")
        finally:
            reset_current_turn(token)

        assert [entry["path"] for entry in ls_result.entries or []] == ["/docs/visible.md"]
        assert [match["path"] for match in glob_result.matches or []] == ["/docs/visible.md"]
        assert ls_result.error is None
        assert glob_result.error is None
        assert glob_result.truncated is False

    def test_non_admin_grep_and_ls_skip_secret_bearing_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "safe.txt").write_text("needle safe\n", encoding="utf-8")
        (repo / "private.txt").write_text(
            "needle private\nghp_" + "a" * 30 + "\n", encoding="utf-8",
        )
        monkeypatch.setenv("MIMIR_HOME", str(tmp_path / "home"))
        auth = AuthContext(
            principal="u", canonical_principal="u", roles=("user",),
            event_ingress=None, trigger="user_message", channel_id="c",
            interactivity=None, enforcement_enabled=True,
        )
        token = set_current_turn(SimpleNamespace(turn_id="read-filter", auth_context=auth))
        try:
            backend = _RootAwareFilesystemBackend(root_dir=repo, virtual_mode=True)
            grep_paths = {
                m["path"]
                for m in backend.grep(
                    "needle", path="/", before_context=1, after_context=1,
                ).matches or []
            }
            ls_names = {
                Path(e["path"].rstrip("/")).name for e in backend.ls("/").entries or []
            }
        finally:
            reset_current_turn(token)

        assert grep_paths == {"/safe.txt"}
        assert ls_names == {"safe.txt"}

    @pytest.mark.parametrize("home_location", ["route", "tmp"])
    def test_non_admin_home_collections_surface_only_admitted_subtrees(
        self,
        home_location: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        if home_location == "tmp":
            route_root = Path(tempfile.mkdtemp(prefix="mimir-home-collection-", dir="/tmp"))
        else:
            route_root = tmp_path / "route"
        home = route_root / "home"
        for dirname in (
            "state",
            "logs",
            "messages",
            "attachments",
            "scratch",
            "memory",
            "conversation_history",
        ):
            (home / dirname).mkdir(parents=True)
        (home / "state" / "visible.txt").write_text("needle visible\n", encoding="utf-8")
        for dirname in (
            "logs",
            "messages",
            "attachments",
            "scratch",
            "memory",
            "conversation_history",
        ):
            (home / dirname / "hidden.txt").write_text(
                f"needle hidden {dirname}\n", encoding="utf-8",
            )
        # `attachments/` is no longer an admitted home root - only the nested
        # `attachments/fetch-cache` is - and the collection walker does not
        # descend into a non-admitted parent. So nothing under `attachments`
        # surfaces here, including the inbound uploads that must never surface.
        (home / "attachments" / "inbound").mkdir(parents=True)
        (home / "attachments" / "inbound" / "hidden.txt").write_text(
            "needle hidden inbound\n", encoding="utf-8",
        )
        monkeypatch.setenv("MIMIR_HOME", str(home))
        auth = AuthContext(
            principal="u", canonical_principal="u", roles=("user",),
            event_ingress=None, trigger="user_message", channel_id="c",
            interactivity=None, enforcement_enabled=True,
        )
        token = set_current_turn(SimpleNamespace(turn_id="home-filter", auth_context=auth))
        try:
            backend = _RootAwareFilesystemBackend(root_dir=route_root, virtual_mode=True)
            grep_paths = {m["path"] for m in backend.grep("needle", path="/").matches or []}
            glob_paths = {m["path"] for m in backend.glob("**/*.txt", path="/").matches or []}
            ls_names = {
                Path(e["path"].rstrip("/")).name
                for e in backend.ls("/home").entries or []
            }
        finally:
            reset_current_turn(token)
            if home_location == "tmp":
                import shutil

                shutil.rmtree(route_root, ignore_errors=True)

        assert grep_paths == {
            "/home/memory/hidden.txt",
            "/home/state/visible.txt",
        }
        assert glob_paths == {
            "/home/memory/hidden.txt",
            "/home/state/visible.txt",
        }
        assert ls_names == {"memory", "state"}


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
        assert w.error == (
            "Write blocked: path contains a symbolic-link or non-directory component."
        )
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

    def test_programmatic_delete_methods_remain_inherited(self) -> None:
        from deepagents.backends.composite import CompositeBackend

        assert FileToolRouter.delete is CompositeBackend.delete
        assert FileToolRouter.adelete is CompositeBackend.adelete

    def test_broad_grep_provenance_covers_all_matches_in_any_route_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        home = _split_home(tmp_path)
        home_file = home / "state" / "home.txt"
        home_file.write_text("fanout needle\n")
        first = tmp_path / "first"
        first.mkdir()
        first_file = first / "first.txt"
        first_file.write_text("fanout needle\n")
        second = tmp_path / "second"
        second.mkdir()
        second_file = second / "second.txt"
        second_file.write_text("fanout needle\n")
        monkeypatch.setenv("MIMIR_HOME", str(home))
        auth = AuthContext(
            principal="user:test", canonical_principal="test", roles=("admin",),
            event_ingress=None, trigger="user_message", channel_id="channel",
            interactivity=None, enforcement_enabled=True,
        )
        turn_token = set_current_turn(SimpleNamespace(
            turn_id="fanout-grep", auth_context=auth,
        ))
        observed = []
        try:
            for roots in ((first, second), (second, first)):
                router = FileToolRouter(
                    default=WriteGuardBackend(root_dir=home, writable_dirs=["state"]),
                    routes=build_file_tool_routes([(str(root), "rw") for root in roots]),
                )
                capture_token = begin_protected_result_capture()
                result = router.grep("fanout needle")
                provenance = end_protected_result_capture(capture_token)

                assert result.error is None
                assert provenance is not None
                assert {source.resource_id for source in provenance.sources} == {
                    str(home_file.resolve()),
                    str(first_file.resolve()),
                    str(second_file.resolve()),
                }
                labels = classify_protected_result(
                    "grep", {}, auth,
                    ToolAuthorization(
                        tool_name="grep", decision=OperationDecision.RESOURCE_SCOPED,
                        allowed=True, flow_direction=ToolFlowDirection.SOURCE,
                    ),
                    result=result, provenance=provenance,
                )
                assert labels is not None
                assert labels.has_untrusted_active_ingest is True
                observed.append(frozenset(labels.sources))
        finally:
            reset_current_turn(turn_token)

        assert observed[0] == observed[1]

    @pytest.mark.asyncio
    async def test_broad_glob_and_aglob_keep_home_taint_when_last_route_is_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        home = _split_home(tmp_path)
        home_file = home / "state" / "untrusted.txt"
        home_file.write_text("content\n")
        populated = tmp_path / "populated"
        populated.mkdir()
        populated_file = populated / "outside.txt"
        populated_file.write_text("content\n")
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.setenv("MIMIR_HOME", str(home))
        auth = AuthContext(
            principal="user:test", canonical_principal="test", roles=("admin",),
            event_ingress=None, trigger="user_message", channel_id="channel",
            interactivity=None, enforcement_enabled=True,
        )
        router = FileToolRouter(
            default=WriteGuardBackend(root_dir=home, writable_dirs=["state"]),
            routes=build_file_tool_routes([
                (str(populated), "rw"),
                (str(empty), "rw"),
            ]),
        )
        turn_token = set_current_turn(SimpleNamespace(
            turn_id="fanout-glob", auth_context=auth,
        ))
        try:
            for method in (router.glob, router.aglob):
                capture_token = begin_protected_result_capture()
                result = method("**/*.txt")
                if method == router.aglob:
                    result = await result
                provenance = end_protected_result_capture(capture_token)

                assert result.error is None
                assert provenance is not None
                assert {source.resource_id for source in provenance.sources} == {
                    str(home_file.resolve()), str(populated_file.resolve()),
                }
                labels = classify_protected_result(
                    "glob", {}, auth,
                    ToolAuthorization(
                        tool_name="glob", decision=OperationDecision.RESOURCE_SCOPED,
                        allowed=True, flow_direction=ToolFlowDirection.SOURCE,
                    ),
                    result=result, provenance=provenance,
                )
                assert labels is not None
                assert labels.has_untrusted_active_ingest is True
        finally:
            reset_current_turn(turn_token)

    def test_broad_glob_with_no_matches_is_authoritatively_empty(
        self, tmp_path: Path,
    ) -> None:
        home = _split_home(tmp_path)
        empty = tmp_path / "empty"
        empty.mkdir()
        router = FileToolRouter(
            default=WriteGuardBackend(root_dir=home, writable_dirs=["state"]),
            routes=build_file_tool_routes([(str(empty), "rw")]),
        )

        capture_token = begin_protected_result_capture()
        result = router.glob("**/*.does-not-exist")
        provenance = end_protected_result_capture(capture_token)

        assert result.error is None
        assert provenance is not None
        assert provenance.sources == ()
        assert classify_protected_result(
            "glob", {}, None,
            ToolAuthorization(
                tool_name="glob", decision=OperationDecision.RESOURCE_SCOPED,
                allowed=True, flow_direction=ToolFlowDirection.SOURCE,
            ),
            result=result, provenance=provenance,
        ) is None

    def test_unresolved_collection_path_invalidates_prior_route_provenance(
        self, tmp_path: Path,
    ) -> None:
        home = _split_home(tmp_path)
        home_file = home / "state" / "home.txt"
        home_file.write_text("content\n")
        route = tmp_path / "route"
        route.mkdir()
        broken = route / "broken.txt"
        broken.symlink_to(route / "missing.txt")
        home_backend = WriteGuardBackend(root_dir=home, writable_dirs=["state"])
        routes = build_file_tool_routes([(str(route), "rw")])
        router = FileToolRouter(
            default=home_backend,
            routes=routes,
        )

        capture_token = begin_protected_result_capture()
        home_backend._fs._publish_read_paths(["/state/home.txt"])
        routes[str(route) + "/"]._publish_read_paths([str(broken)])
        provenance = end_protected_result_capture(capture_token)

        assert router.routes[str(route) + "/"] is routes[str(route) + "/"]
        assert home_file.exists()
        assert provenance is None

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

        capture_token = begin_protected_result_capture()
        result = await router.agrep("needle")
        provenance = end_protected_result_capture(capture_token)
        paths = {m["path"] for m in (result.matches or [])}

        assert result.error is None
        assert "/state/home.txt" in paths
        assert f"{repo}/repo.txt" in paths
        assert len([p for p in paths if p.startswith(str(data))]) == 1
        assert provenance is not None
        assert {source.resource_id for source in provenance.sources} == {
            str((home / path.lstrip("/")).resolve())
            if path.startswith("/state/") else str(Path(path).resolve())
            for path in paths
        }


def test_denial_buffer_is_bounded(tmp_path: Path):
    """Denials with no turn_id (non-turn callables) and denials from
    crashed turns are never drained by the turn-scoped path — the buffer
    drops oldest past 512 so it can't grow for process lifetime."""
    b = WriteGuardBackend(root_dir=tmp_path, writable_dirs=["state"])
    for i in range(600):
        b._record_denial("write", f"/logs/blocked-{i}.txt")
    assert len(b._denials) == 512
    assert b._denials[0]["file_path"] == "/logs/blocked-88.txt"
    assert b._denials[-1]["file_path"] == "/logs/blocked-599.txt"


def test_every_exclusion_site_reports_which_tool_withheld_the_path():
    """No read-withholding path may be silent (#1012).

    ``_is_excluded`` emits ``hard_boundary_denied`` only for the caller that
    names its tool. The first version of this made ``tool`` optional and left
    ``_ripgrep_search`` passing nothing, so the *primary* grep path recorded
    nothing while the instrumented one — the Python walker — is only the
    fallback taken when ``rg`` is absent. ``rg`` is a pinned executable in
    production, so the covered path was the one that does not run and the tests
    passed anyway.

    This is a source check rather than a behavioural one because reaching
    ``_ripgrep_search`` requires ``rg`` on the test host; the point is that a
    call site cannot be added without naming its tool. The keyword-only
    ``tool: str`` with no default is the real guard — this test explains why it
    must stay that way.
    """
    import ast
    import inspect

    from mimir import readonly_backend

    source = inspect.getsource(readonly_backend)
    tree = ast.parse(source)

    signature = inspect.signature(readonly_backend._RootAwareFilesystemBackend._is_excluded)
    tool_param = signature.parameters["tool"]
    assert tool_param.default is inspect.Parameter.empty, (
        "_is_excluded(tool=...) must stay required; a default lets a new call "
        "site withhold a path silently, which is the defect this guards"
    )

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "_is_excluded"):
            continue
        if not any(kw.arg == "tool" for kw in node.keywords):
            offenders.append(f"readonly_backend.py:{node.lineno}")

    assert not offenders, (
        "these _is_excluded call sites withhold a path without recording which "
        "tool did it: " + ", ".join(offenders)
    )
