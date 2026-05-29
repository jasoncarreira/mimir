"""Tests for non-poller skill-memory load injection (chainlink #266, slice 3).

The middleware appends a skill's recorded learnings to a ``read_file`` of
its ``<skill>/SKILL.md`` so the model sees them inline when it loads a
skill on a non-poller turn. Coverage:
  - _skill_from_path parsing (valid / bare / non-skill / trailing / win)
  - augments a SKILL.md read for a skill WITH learnings (heading + nudge)
  - no-op: skill with no learnings, non-SKILL.md read, non-read_file tool,
    error-status result, no SagaStore installed
  - both the sync and async middleware paths
  - the handler is always delegated to (read still happens)
"""
from __future__ import annotations

import pytest
from langchain.agents.middleware import ToolCallRequest
from langchain_core.messages import ToolMessage

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
    def test_valid(self):
        assert _skill_from_path("/home/x/skills/memory/SKILL.md") == "memory"

    def test_bare_filename_is_none(self):
        assert _skill_from_path("SKILL.md") is None

    def test_non_skill_file_is_none(self):
        assert _skill_from_path("/home/x/skills/memory/README.md") is None

    def test_trailing_slash_tolerated(self):
        assert _skill_from_path("/a/github-poller/SKILL.md/") == "github-poller"

    def test_windows_separators(self):
        assert _skill_from_path(r"C:\skills\alerts\SKILL.md") == "alerts"

    def test_empty(self):
        assert _skill_from_path("") is None


# ── fixtures: real SagaStore via stub provider ───────────────────────


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
    async def test_augments_skill_md_with_learnings(self, store):
        await _add_learning(store, "memory", "failure-mode", "trips on empty input")
        mw = SkillMemoryInjectionMiddleware()
        _, ahandler, calls = _make_handler(
            _read_result("1\tORIGINAL SKILL BODY")
        )
        out = await mw.awrap_tool_call(
            _read_request("/home/x/skills/memory/SKILL.md"), ahandler,
        )
        assert calls["n"] == 1  # read still happened
        assert "ORIGINAL SKILL BODY" in out.content
        assert _LEARNINGS_HEADING in out.content
        assert "[failure-mode] trips on empty input" in out.content
        assert "saga_record_skill_learning" in out.content  # write nudge

    @pytest.mark.asyncio
    async def test_no_learnings_leaves_content_unchanged(self, store):
        mw = SkillMemoryInjectionMiddleware()
        _, ahandler, _ = _make_handler(_read_result("BODY"))
        out = await mw.awrap_tool_call(
            _read_request("/x/skills/never-used/SKILL.md"), ahandler,
        )
        assert out.content == "BODY"

    @pytest.mark.asyncio
    async def test_non_skill_md_read_unchanged(self, store):
        await _add_learning(store, "memory", "tip", "x")
        mw = SkillMemoryInjectionMiddleware()
        _, ahandler, _ = _make_handler(_read_result("SOME OTHER FILE"))
        out = await mw.awrap_tool_call(
            _read_request("/x/skills/memory/README.md"), ahandler,
        )
        assert out.content == "SOME OTHER FILE"

    @pytest.mark.asyncio
    async def test_non_read_file_tool_unchanged(self, store):
        await _add_learning(store, "memory", "tip", "x")
        mw = SkillMemoryInjectionMiddleware()
        _, ahandler, _ = _make_handler(_read_result("WHATEVER"))
        out = await mw.awrap_tool_call(
            _read_request("/x/skills/memory/SKILL.md", tool_name="write_file"),
            ahandler,
        )
        assert out.content == "WHATEVER"

    @pytest.mark.asyncio
    async def test_error_status_unchanged(self, store):
        await _add_learning(store, "memory", "tip", "x")
        mw = SkillMemoryInjectionMiddleware()
        _, ahandler, _ = _make_handler(
            _read_result("Error: not found", status="error")
        )
        out = await mw.awrap_tool_call(
            _read_request("/x/skills/memory/SKILL.md"), ahandler,
        )
        assert out.content == "Error: not found"

    @pytest.mark.asyncio
    async def test_no_client_best_effort_unchanged(self, monkeypatch):
        prev = _MEMORY_STATE.get("client")
        _MEMORY_STATE["client"] = None
        try:
            mw = SkillMemoryInjectionMiddleware()
            _, ahandler, _ = _make_handler(_read_result("BODY"))
            out = await mw.awrap_tool_call(
                _read_request("/x/skills/memory/SKILL.md"), ahandler,
            )
            assert out.content == "BODY"
        finally:
            _MEMORY_STATE["client"] = prev


# ── sync path ────────────────────────────────────────────────────────


class TestSyncInjection:
    @pytest.mark.asyncio
    async def test_sync_augments_with_learnings(self, store):
        # store() is async; populate, then exercise the sync wrap path.
        await _add_learning(store, "alerts", "tip", "batch the pages")
        mw = SkillMemoryInjectionMiddleware()
        handler, _, calls = _make_handler(_read_result("ALERTS BODY"))
        out = mw.wrap_tool_call(
            _read_request("/x/skills/alerts/SKILL.md"), handler,
        )
        assert calls["n"] == 1
        assert "ALERTS BODY" in out.content
        assert "[tip] batch the pages" in out.content
        assert "saga_record_skill_learning" in out.content

    @pytest.mark.asyncio
    async def test_sync_non_read_file_unchanged(self, store):
        await _add_learning(store, "alerts", "tip", "x")
        mw = SkillMemoryInjectionMiddleware()
        handler, _, _ = _make_handler(_read_result("BODY"))
        out = mw.wrap_tool_call(
            _read_request("/x/skills/alerts/SKILL.md", tool_name="glob"),
            handler,
        )
        assert out.content == "BODY"


# ── slice 6: injected IDs land on the turn for synthesis voting ──────


class TestInjectedIdCapture:
    @pytest.mark.asyncio
    async def test_records_injected_ids_onto_turn_context(self, store):
        """After augmenting a SKILL.md read, the injected learning atom IDs
        must be recorded on the active turn's injected_skill_atom_ids so
        run_turn folds them into the TurnRecord for synthesis voting."""
        import time as _time
        from mimir._context import set_current_turn, reset_current_turn
        from mimir.models import TurnContext

        sl = await _add_learning(store, "memory", "tip", "a useful tip")
        ctx = TurnContext(
            turn_id="t", session_id="c", trigger="user_message",
            channel_id="c", started_at=_time.monotonic(),
        )
        tok = set_current_turn(ctx)
        try:
            mw = SkillMemoryInjectionMiddleware()
            _, ahandler, _ = _make_handler(_read_result("BODY"))
            out = await mw.awrap_tool_call(
                _read_request("/x/skills/memory/SKILL.md"), ahandler,
            )
            assert "a useful tip" in out.content  # augmentation happened
            assert ctx.injected_skill_atom_ids == [sl["atom_id"]]
        finally:
            reset_current_turn(tok)

    @pytest.mark.asyncio
    async def test_no_ids_recorded_when_no_learnings(self, store):
        import time as _time
        from mimir._context import set_current_turn, reset_current_turn
        from mimir.models import TurnContext

        ctx = TurnContext(
            turn_id="t", session_id="c", trigger="user_message",
            channel_id="c", started_at=_time.monotonic(),
        )
        tok = set_current_turn(ctx)
        try:
            mw = SkillMemoryInjectionMiddleware()
            _, ahandler, _ = _make_handler(_read_result("BODY"))
            await mw.awrap_tool_call(
                _read_request("/x/skills/never-used/SKILL.md"), ahandler,
            )
            assert ctx.injected_skill_atom_ids == []
        finally:
            reset_current_turn(tok)
