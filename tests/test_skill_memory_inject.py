"""Tests for non-poller skill-memory load injection (chainlink #266, slice 3).

The middleware appends a skill's recorded learnings to a ``read_file`` of
its ``<skill>/SKILL.md`` so the model sees them inline when it loads a
skill on a non-poller turn. Coverage:
  - _skill_from_path accepts only skills under registered source roots
  - augments a SKILL.md read for a skill WITH learnings (heading + nudge)
  - no-op: skill with no learnings, non-SKILL.md read, non-read_file tool,
    error-status result, no SagaStore installed
  - both the sync and async middleware paths
  - the handler is always delegated to (read still happens)
"""
from __future__ import annotations

import time

import pytest
from langchain.agents.middleware import ToolCallRequest
from langchain_core.messages import ToolMessage

from mimir._context import reset_current_turn, set_current_turn
from mimir.models import AuthContext, InformationFlowLabels, TurnContext
from mimir.saga.client import SagaStore
from mimir.skill_memory import (
    SKILL_LEARNING_SOURCE_TYPE,
    _LEARNINGS_HEADING,
    build_metadata,
)
from mimir.tools import skill_memory_inject as smi
from mimir.tools.memory import _MEMORY_STATE
from mimir.tools.skill_memory_inject import (
    SkillMemoryInjectionMiddleware,
    _skill_from_path,
)


# ── _skill_from_path ─────────────────────────────────────────────────


class TestSkillFromPath:
    def test_registered_skill(self, tmp_path):
        root = (tmp_path / "skills").resolve()
        assert _skill_from_path(
            str(root / "memory" / "SKILL.md"), (root,)
        ) == "memory"

    def test_bare_filename_is_none(self, tmp_path):
        assert _skill_from_path("SKILL.md", (tmp_path.resolve(),)) is None

    def test_non_skill_file_is_none(self, tmp_path):
        root = tmp_path.resolve()
        assert _skill_from_path(str(root / "memory" / "README.md"), (root,)) is None

    def test_unregistered_same_named_directory_is_none(self, tmp_path):
        root = (tmp_path / "registered").resolve()
        outside = tmp_path / "model-created" / "memory" / "SKILL.md"
        assert _skill_from_path(str(outside), (root,)) is None

    def test_nested_path_is_not_a_catalog_entry(self, tmp_path):
        root = tmp_path.resolve()
        nested = root / "model-created" / "memory" / "SKILL.md"
        assert _skill_from_path(str(nested), (root,)) is None

    def test_empty(self):
        assert _skill_from_path("", ()) is None


# ── fixtures: real SagaStore via stub provider ───────────────────────


ADMIN_AUTH = AuthContext(
    principal="test-admin",
    canonical_principal="test-admin",
    roles=("admin",),
    event_ingress="test",
    trigger="user_message",
    channel_id="test-channel",
    interactivity=None,
)


def _turn_context(*, auth_context: AuthContext = ADMIN_AUTH) -> TurnContext:
    return TurnContext(
        turn_id="t",
        session_id="test-channel",
        trigger="user_message",
        channel_id="test-channel",
        started_at=time.monotonic(),
        auth_context=auth_context,
    )


def _patch_provider(monkeypatch, dim: int = 4):
    class _StubProvider:
        def embed(self, text, *, input_type="passage"):
            h = abs(hash(text)) % 1000
            return [float((h + i) % 17) / 17.0 for i in range(dim)]

        def dimensions(self):
            return dim

    monkeypatch.setattr(
        "mimir.saga.embeddings.get_provider", lambda: _StubProvider()
    )

    def fake_get_config():
        def cfg(section, key, default=None):
            return {
                ("embedding", "max_input_chars"): 2000,
                ("embedding", "provider"): "stub",
                ("embedding", "model"): f"stub-{dim}d",
            }.get((section, key), default)
        return cfg

    monkeypatch.setattr("mimir.saga._config_io.get_config", fake_get_config)


@pytest.fixture
def store(tmp_path, monkeypatch):
    _patch_provider(monkeypatch)
    s = SagaStore(db_path=tmp_path / "test.saga.db", embedding_dim=4)
    prev = _MEMORY_STATE.get("client")
    _MEMORY_STATE["client"] = s
    yield s
    _MEMORY_STATE["client"] = prev


@pytest.fixture
def skill_source(tmp_path):
    root = tmp_path / "skills"
    for skill in ("memory", "alerts", "never-used"):
        skill_dir = root / skill
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(f"# {skill}\n", encoding="utf-8")
    return root.resolve()


def _middleware(skill_source):
    return SkillMemoryInjectionMiddleware([str(skill_source)])


def _skill_path(skill_source, skill: str) -> str:
    return str(skill_source / skill / "SKILL.md")


async def _add_learning(store, skill, kind, content):
    return await store.store(
        content, source_type=SKILL_LEARNING_SOURCE_TYPE,
        metadata=build_metadata(skill, kind),
    )


def _read_request(file_path: str, tool_name: str = "read_file") -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={
            "name": tool_name,
            "args": {"file_path": file_path},
            "id": "tc-1",
            "type": "tool_call",
        },
        tool=None,
        state=None,
        runtime=None,  # type: ignore[arg-type]
    )


def _read_result(content: str, status: str = "success") -> ToolMessage:
    return ToolMessage(
        content=content, name="read_file", tool_call_id="tc-1", status=status,
    )


def _make_handler(result: ToolMessage):
    calls = {"n": 0}

    async def ahandler(req):
        calls["n"] += 1
        return result

    def handler(req):
        calls["n"] += 1
        return result

    return handler, ahandler, calls


# ── async path ───────────────────────────────────────────────────────


class TestAsyncInjection:
    @pytest.mark.asyncio
    async def test_augments_skill_md_with_learnings(self, store, skill_source):
        await _add_learning(store, "memory", "failure-mode", "trips on empty input")
        token = set_current_turn(_turn_context())
        try:
            mw = _middleware(skill_source)
            _, ahandler, calls = _make_handler(
                _read_result("1\tORIGINAL SKILL BODY")
            )
            out = await mw.awrap_tool_call(
                _read_request(_skill_path(skill_source, "memory")), ahandler,
            )
        finally:
            reset_current_turn(token)
        assert calls["n"] == 1  # read still happened
        assert "ORIGINAL SKILL BODY" in out.content
        assert _LEARNINGS_HEADING in out.content
        assert "[failure-mode] trips on empty input" in out.content
        assert "saga_record_skill_learning" in out.content  # write nudge

    @pytest.mark.asyncio
    async def test_no_learnings_leaves_content_unchanged(self, store, skill_source):
        mw = _middleware(skill_source)
        _, ahandler, _ = _make_handler(_read_result("BODY"))
        out = await mw.awrap_tool_call(
            _read_request(_skill_path(skill_source, "never-used")), ahandler,
        )
        assert out.content == "BODY"

    @pytest.mark.asyncio
    async def test_non_skill_md_read_unchanged(self, store, skill_source):
        await _add_learning(store, "memory", "tip", "x")
        mw = _middleware(skill_source)
        _, ahandler, _ = _make_handler(_read_result("SOME OTHER FILE"))
        out = await mw.awrap_tool_call(
            _read_request(str(skill_source / "memory" / "README.md")), ahandler,
        )
        assert out.content == "SOME OTHER FILE"

    @pytest.mark.asyncio
    async def test_unregistered_same_named_skill_does_not_inject(
        self, store, skill_source, tmp_path,
    ):
        await _add_learning(store, "memory", "tip", "registered learning")
        mw = _middleware(skill_source)
        _, ahandler, _ = _make_handler(_read_result("MODEL-CREATED BODY"))
        unregistered = tmp_path / "model-created" / "memory" / "SKILL.md"

        out = await mw.awrap_tool_call(_read_request(str(unregistered)), ahandler)

        assert out.content == "MODEL-CREATED BODY"

    @pytest.mark.asyncio
    async def test_non_read_file_tool_unchanged(self, store, skill_source):
        await _add_learning(store, "memory", "tip", "x")
        mw = _middleware(skill_source)
        _, ahandler, _ = _make_handler(_read_result("WHATEVER"))
        out = await mw.awrap_tool_call(
            _read_request(_skill_path(skill_source, "memory"), tool_name="write_file"),
            ahandler,
        )
        assert out.content == "WHATEVER"

    @pytest.mark.asyncio
    async def test_error_status_unchanged(self, store, skill_source):
        await _add_learning(store, "memory", "tip", "x")
        mw = _middleware(skill_source)
        _, ahandler, _ = _make_handler(
            _read_result("Error: not found", status="error")
        )
        out = await mw.awrap_tool_call(
            _read_request(_skill_path(skill_source, "memory")), ahandler,
        )
        assert out.content == "Error: not found"

    @pytest.mark.asyncio
    async def test_no_client_best_effort_unchanged(self, monkeypatch, skill_source):
        prev = _MEMORY_STATE.get("client")
        _MEMORY_STATE["client"] = None
        try:
            mw = _middleware(skill_source)
            _, ahandler, _ = _make_handler(_read_result("BODY"))
            out = await mw.awrap_tool_call(
                _read_request(_skill_path(skill_source, "memory")), ahandler,
            )
            assert out.content == "BODY"
        finally:
            _MEMORY_STATE["client"] = prev


# ── sync path ────────────────────────────────────────────────────────


class TestSyncInjection:
    @pytest.mark.asyncio
    async def test_sync_augments_with_learnings(self, store, skill_source):
        # store() is async; populate, then exercise the sync wrap path.
        await _add_learning(store, "alerts", "tip", "batch the pages")
        token = set_current_turn(_turn_context())
        try:
            mw = _middleware(skill_source)
            handler, _, calls = _make_handler(_read_result("ALERTS BODY"))
            out = mw.wrap_tool_call(
                _read_request(_skill_path(skill_source, "alerts")), handler,
            )
        finally:
            reset_current_turn(token)
        assert calls["n"] == 1
        assert "ALERTS BODY" in out.content
        assert "[tip] batch the pages" in out.content
        assert "saga_record_skill_learning" in out.content

    @pytest.mark.asyncio
    async def test_sync_non_read_file_unchanged(self, store, skill_source):
        await _add_learning(store, "alerts", "tip", "x")
        mw = _middleware(skill_source)
        handler, _, _ = _make_handler(_read_result("BODY"))
        out = mw.wrap_tool_call(
            _read_request(_skill_path(skill_source, "alerts"), tool_name="glob"),
            handler,
        )
        assert out.content == "BODY"


# ── slice 6: injected IDs land on the turn for synthesis voting ──────


class TestInjectedIdCapture:
    @pytest.mark.asyncio
    async def test_records_injected_ids_onto_turn_context(self, store, skill_source):
        """After augmenting a SKILL.md read, the injected learning atom IDs
        must be recorded on the active turn's injected_skill_atom_ids so
        run_turn folds them into the TurnRecord for synthesis voting."""
        sl = await _add_learning(store, "memory", "tip", "a useful tip")
        ctx = _turn_context()
        tok = set_current_turn(ctx)
        try:
            mw = _middleware(skill_source)
            _, ahandler, _ = _make_handler(_read_result("BODY"))
            out = await mw.awrap_tool_call(
                _read_request(_skill_path(skill_source, "memory")), ahandler,
            )
            assert "a useful tip" in out.content  # augmentation happened
            assert ctx.injected_skill_atom_ids == [sl["atom_id"]]
            sources = ctx.auth_context.ifc_state.current().sources
            learning_source = next(
                source for source in sources
                if source.resource_id == f"atom:{sl['atom_id']}"
            )
            assert learning_source.domain == "saga"
            assert learning_source.bridge_instance == "saga"
            assert learning_source.sensitivity == "private"
            assert learning_source.source_kind == "auto_recall"
            assert learning_source.integrity == "untrusted"
            assert learning_source.integrity_effect == "informational"
            assert learning_source.authorized_principals == frozenset({
                "legacy_admin", "test-admin",
            })
        finally:
            reset_current_turn(tok)

    @pytest.mark.asyncio
    async def test_no_ids_recorded_when_no_learnings(self, store, skill_source):
        import time as _time
        from mimir._context import set_current_turn, reset_current_turn
        from mimir.models import TurnContext

        ctx = TurnContext(
            turn_id="t", session_id="c", trigger="user_message",
            channel_id="c", started_at=_time.monotonic(),
        )
        tok = set_current_turn(ctx)
        try:
            mw = _middleware(skill_source)
            _, ahandler, _ = _make_handler(_read_result("BODY"))
            await mw.awrap_tool_call(
                _read_request(_skill_path(skill_source, "never-used")), ahandler,
            )
            assert ctx.injected_skill_atom_ids == []
            assert ctx.ifc_labels is None
        finally:
            reset_current_turn(tok)
