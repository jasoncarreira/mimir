from __future__ import annotations

import base64
import hashlib
import logging
import re
from pathlib import Path
from types import MethodType, SimpleNamespace

import deepagents.graph as deepagents_graph
import pytest
from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend
from deepagents.backends.protocol import (
    EditResult,
    FileDownloadResponse,
    FileUploadResponse,
    WriteResult,
)
from deepagents.middleware.summarization import SummarizationMiddleware
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage

from mimir import _deepagents_summarization as offload_patch
from mimir._context import reset_current_turn, set_current_turn
from mimir.config import Config
from mimir.models import AuthContext
from mimir.readonly_backend import WriteGuardBackend
from mimir.subagents import build_mimir_subagents


_HISTORY_PATH = "/conversation_history/thread-973.md"
_MEDIA_BYTES = b"inline image bytes"
_MEDIA_HASH = hashlib.sha256(_MEDIA_BYTES).hexdigest()[:16]


def _middleware(
    path: str = _HISTORY_PATH,
    *,
    media_prefix: str = "/conversation_history/media",
) -> SummarizationMiddleware:
    offload_patch.install_offload_traceback_logging_patch()
    middleware = object.__new__(SummarizationMiddleware)
    # deepagents 0.7.10 passes session_id to _get_history_path; the stub ignores it
    # and keeps returning the fixture path.
    middleware._get_history_path = MethodType(
        lambda _self, _session_id=None: path, middleware
    )
    middleware._history_path_prefix = str(Path(path).parent)
    middleware._media_prefix = media_prefix
    return middleware


def _inline_image_message() -> HumanMessage:
    return HumanMessage(
        content=[
            {
                "type": "image",
                "base64": base64.b64encode(_MEDIA_BYTES).decode(),
                "mime_type": "image/png",
            }
        ]
    )


def _assert_history(history: str, first: str, second: str) -> None:
    assert history.count("## Summarized at ") == 2
    timestamps = re.findall(r"## Summarized at ([^\n]+)", history)
    assert len(timestamps) == 2
    assert all(timestamp.endswith("+00:00") for timestamp in timestamps)
    assert f'<message type="human">{first}</message>' in history
    assert f'<message type="ai">{second}</message>' in history
    assert "discarded previous summary" not in history


def test_sync_history_offload_creates_and_appends_raw_xml(tmp_path: Path) -> None:
    backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
    middleware = _middleware()
    previous_summary = HumanMessage(
        content="discarded previous summary",
        additional_kwargs={"lc_source": "summarization"},
    )

    first = middleware._offload_to_backend(
        backend,
        [HumanMessage(content="first <tag>"), previous_summary], "session-offload",
    )
    second = middleware._offload_to_backend(
        backend,
        [AIMessage(content="second")], "session-offload",
    )

    assert first == second == _HISTORY_PATH
    history = (tmp_path / "conversation_history" / "thread-973.md").read_text()
    _assert_history(history, "first &lt;tag&gt;", "second")


async def test_async_history_offload_creates_and_appends_raw_xml(
    tmp_path: Path,
) -> None:
    backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
    middleware = _middleware()
    previous_summary = HumanMessage(
        content="discarded previous summary",
        additional_kwargs={"lc_source": "summarization"},
    )

    first = await middleware._aoffload_to_backend(
        backend,
        [HumanMessage(content="first"), previous_summary], "session-offload",
    )
    second = await middleware._aoffload_to_backend(
        backend,
        [AIMessage(content="second")], "session-offload",
    )

    assert first == second == _HISTORY_PATH
    history = (tmp_path / "conversation_history" / "thread-973.md").read_text()
    _assert_history(history, "first", "second")


def test_production_backend_offloads_and_preserves_read_boundary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.delenv("MIMIR_FOLDERS", raising=False)
    config = Config.from_env()
    backend = WriteGuardBackend(
        root_dir=config.home,
        writable_dirs=config.writable_dirs,
        guard_outside_root=True,
    )
    middleware = _middleware()
    auth = AuthContext(
        principal="user",
        canonical_principal="user",
        roles=("user",),
        event_ingress=None,
        trigger="user_message",
        channel_id="channel",
        interactivity=None,
        enforcement_enabled=False,
    )
    events = []
    monkeypatch.setattr(
        "mimir.tools.budget_gate._emit_event_sync",
        lambda kind, **fields: events.append((kind, fields)),
    )

    token = set_current_turn(SimpleNamespace(turn_id="offload", auth_context=auth))
    try:
        first = middleware._offload_to_backend(backend, [HumanMessage(content="first")], "session-offload")
        second = middleware._offload_to_backend(backend, [HumanMessage(content="second")], "session-offload")
        direct_read = backend.read(_HISTORY_PATH)
    finally:
        reset_current_turn(token)

    history = tmp_path / "conversation_history" / "thread-973.md"
    assert first == second == _HISTORY_PATH
    assert history.read_text().count("## Summarized at ") == 2
    assert '<message type="human">first</message>' in history.read_text()
    assert '<message type="human">second</message>' in history.read_text()
    assert direct_read.error == (
        "Read denied: mimir_home_read_boundary. Use an allowed state path instead."
    )
    hard = next(fields for kind, fields in events if kind == "hard_boundary_denied")
    assert hard["boundary"] == "protected_read_policy"
    assert hard["reason"] == "mimir_home_read_boundary"
    assert hard["target"] == str(history)
    assert hard["trigger"] == "user_message"

    denied = backend.write("/not_writable/outside.md", "blocked")
    assert "Write blocked" in (denied.error or "")
    assert not (tmp_path / "not_writable" / "outside.md").exists()


def test_sync_inline_media_delegates_upload_and_rewrites_reference(
    tmp_path: Path,
) -> None:
    backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
    media_prefix = "/artifacts/conversation_history/media"
    middleware = _middleware(media_prefix=media_prefix)

    rewritten, failed = middleware._offload_inline_media(
        backend, [_inline_image_message()]
    )

    media_path = f"{media_prefix}/{_MEDIA_HASH}.png"
    assert failed == 0
    assert rewritten[0].content_blocks == [{"type": "image", "url": media_path}]
    assert (tmp_path / media_path.lstrip("/")).read_bytes() == _MEDIA_BYTES


async def test_async_inline_media_delegates_upload_and_rewrites_reference(
    tmp_path: Path,
) -> None:
    backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
    media_prefix = "/artifacts/conversation_history/media"
    middleware = _middleware(media_prefix=media_prefix)

    rewritten, failed = await middleware._aoffload_inline_media(
        backend, [_inline_image_message()]
    )

    media_path = f"{media_prefix}/{_MEDIA_HASH}.png"
    assert failed == 0
    assert rewritten[0].content_blocks == [{"type": "image", "url": media_path}]
    assert (tmp_path / media_path.lstrip("/")).read_bytes() == _MEDIA_BYTES


class _ExplodingSyncBackend:
    def __init__(self, operation: str) -> None:
        self.operation = operation

    def _raise(self, operation: str) -> None:
        if self.operation == operation:
            raise RuntimeError(f"{operation} unavailable")

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        self._raise("upload_files")
        return [FileUploadResponse(path=path) for path, _content in files]

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        self._raise("download_files")
        content = b"existing history\n" if self.operation == "edit" else None
        error = None if content is not None else "file_not_found"
        return [
            FileDownloadResponse(path=path, content=content, error=error)
            for path in paths
        ]

    def write(self, file_path: str, _content: str) -> WriteResult:
        self._raise("write")
        return WriteResult(path=file_path)

    def edit(
        self,
        file_path: str,
        _old_string: str,
        _new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        self._raise("edit")
        return EditResult(path=file_path, occurrences=1)


@pytest.mark.parametrize(
    ("operation", "expected_result"),
    [
        ("upload_files", "placeholder"),
        ("download_files", _HISTORY_PATH),
        ("write", None),
        ("edit", None),
    ],
)
def test_sync_backend_exceptions_log_traceback_and_remain_nonfatal(
    operation: str,
    expected_result: str | None,
    caplog,
) -> None:
    middleware = _middleware()
    backend = _ExplodingSyncBackend(operation)

    with caplog.at_level(logging.WARNING, logger="deepagents.middleware.summarization"):
        if operation == "upload_files":
            rewritten, failed = middleware._offload_inline_media(
                backend, [_inline_image_message()]
            )
            result = "placeholder"
            assert failed == 1
            assert rewritten[0].content_blocks == [
                {"type": "text", "text": '<image error="failed_to_offload" />'}
            ]
            expected_path = f"/conversation_history/media/{_MEDIA_HASH}.png"
        else:
            result = middleware._offload_to_backend(
                backend, [HumanMessage(content="history")], "session-offload"
            )
            expected_path = _HISTORY_PATH

    assert result == expected_result
    record = next(
        record
        for record in caplog.records
        if f"backend {operation} for {expected_path}" in record.message
    )
    assert record.exc_info is not None
    assert record.exc_info[0] is RuntimeError
    assert f"{operation} unavailable" in caplog.text


class _ExplodingAsyncBackend:
    def __init__(self, operation: str) -> None:
        self.operation = operation

    def _raise(self, operation: str) -> None:
        if self.operation == operation:
            raise RuntimeError(f"{operation} unavailable")

    async def aupload_files(
        self, files: list[tuple[str, bytes]]
    ) -> list[FileUploadResponse]:
        self._raise("aupload_files")
        return [FileUploadResponse(path=path) for path, _content in files]

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        self._raise("adownload_files")
        content = b"existing history\n" if self.operation == "aedit" else None
        error = None if content is not None else "file_not_found"
        return [
            FileDownloadResponse(path=path, content=content, error=error)
            for path in paths
        ]

    async def awrite(self, file_path: str, _content: str) -> WriteResult:
        self._raise("awrite")
        return WriteResult(path=file_path)

    async def aedit(
        self,
        file_path: str,
        _old_string: str,
        _new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        self._raise("aedit")
        return EditResult(path=file_path, occurrences=1)


@pytest.mark.parametrize(
    ("operation", "expected_result"),
    [
        ("aupload_files", "placeholder"),
        ("adownload_files", _HISTORY_PATH),
        ("awrite", None),
        ("aedit", None),
    ],
)
async def test_async_backend_exceptions_log_traceback_and_remain_nonfatal(
    operation: str,
    expected_result: str | None,
    caplog,
) -> None:
    middleware = _middleware()
    backend = _ExplodingAsyncBackend(operation)

    with caplog.at_level(logging.WARNING, logger="deepagents.middleware.summarization"):
        if operation == "aupload_files":
            rewritten, failed = await middleware._aoffload_inline_media(
                backend, [_inline_image_message()]
            )
            result = "placeholder"
            assert failed == 1
            assert rewritten[0].content_blocks == [
                {"type": "text", "text": '<image error="failed_to_offload" />'}
            ]
            expected_path = f"/conversation_history/media/{_MEDIA_HASH}.png"
        else:
            result = await middleware._aoffload_to_backend(
                backend, [HumanMessage(content="history")], "session-offload"
            )
            expected_path = _HISTORY_PATH

    assert result == expected_result
    record = next(
        record
        for record in caplog.records
        if f"backend {operation} for {expected_path}" in record.message
    )
    assert record.exc_info is not None
    assert record.exc_info[0] is RuntimeError
    assert f"{operation} unavailable" in caplog.text


def test_installation_wraps_all_four_methods_once_and_retains_originals() -> None:
    offload_patch.install_offload_traceback_logging_patch()
    method_names = (
        "_offload_inline_media",
        "_aoffload_inline_media",
        "_offload_to_backend",
        "_aoffload_to_backend",
    )
    installed = {
        name: getattr(SummarizationMiddleware, name) for name in method_names
    }

    offload_patch.install_offload_traceback_logging_patch()

    for name, wrapper in installed.items():
        assert getattr(SummarizationMiddleware, name) is wrapper
        assert getattr(wrapper, offload_patch._OFFLOAD_LOGGING_PATCH_MARKER) is True
        assert wrapper.__wrapped__ is not wrapper


def test_real_main_general_and_critic_construction_uses_artifacts_root(
    monkeypatch,
) -> None:
    from deepagents import create_deep_agent

    offload_patch.install_offload_traceback_logging_patch()
    backend = CompositeBackend(
        default=StateBackend(),
        routes={},
        artifacts_root="/artifacts/runtime",
    )
    created_middleware: list[SummarizationMiddleware] = []
    factory_calls: list[dict] = []
    original_factory = deepagents_graph.create_summarization_middleware

    def capture_factory(*args, **kwargs):
        factory_calls.append(kwargs)
        middleware = original_factory(*args, **kwargs)
        created_middleware.append(middleware)
        return middleware

    monkeypatch.setattr(
        deepagents_graph,
        "create_summarization_middleware",
        capture_factory,
    )
    subagents = build_mimir_subagents()

    create_deep_agent(
        model=GenericFakeChatModel(messages=iter([AIMessage(content="done")])),
        backend=backend,
        tools=[],
        system_prompt="summarization construction test",
        subagents=subagents,
    )

    assert [subagent["name"] for subagent in subagents] == [
        "general-purpose",
        "critic-structured",
    ]
    assert len(created_middleware) == 3
    assert all(
        middleware._history_path_prefix
        == "/artifacts/runtime/conversation_history"
        for middleware in created_middleware
    )
    assert all(
        middleware._media_prefix
        == "/artifacts/runtime/conversation_history/media"
        for middleware in created_middleware
    )
    assert all("history_path_prefix" not in kwargs for kwargs in factory_calls)


@pytest.mark.parametrize("is_async", [False, True])
def test_offload_wrappers_forward_upstream_signature_changes(is_async: bool) -> None:
    """The wrappers must not restate upstream's parameter list.

    Their only job is to substitute ``backend`` with a logging proxy; everything
    after it belongs to deepagents and has to pass through untouched.

    deepagents 0.7.10 added a ``session_id`` argument to `_offload_to_backend` and
    `_aoffload_to_backend`. Wrappers hardcoded to ``(self, backend, messages)`` then
    raised `TypeError: ... takes 3 positional arguments but 4 were given` on every
    turn that offloaded, and the pin is `>=0.7.1,<0.8`, so a signature change inside
    that range is expected rather than exceptional -- which is why this asserts
    forwarding rather than any particular arity.

    Both arities are exercised: the `_offload_to_backend` pair take four arguments
    on 0.7.10 while the `_inline_media` pair still take three.
    """
    import asyncio

    from mimir._deepagents_summarization import (
        _wrap_async_offload,
        _wrap_sync_offload,
    )

    seen: dict[str, object] = {}

    if is_async:
        async def original(_self, backend, *args, **kwargs):  # type: ignore[no-untyped-def]
            seen.update(backend=backend, args=args, kwargs=kwargs)
            return "ok"

        wrapped = _wrap_async_offload(original, logging.getLogger(__name__))
        assert asyncio.run(wrapped(None, "raw-backend", ["m"], "session-1")) == "ok"
    else:
        def original(_self, backend, *args, **kwargs):  # type: ignore[no-untyped-def]
            seen.update(backend=backend, args=args, kwargs=kwargs)
            return "ok"

        wrapped = _wrap_sync_offload(original, logging.getLogger(__name__))
        assert wrapped(None, "raw-backend", ["m"], "session-1") == "ok"

    # Everything after ``backend`` arrives unchanged, including the argument that
    # upstream added.
    assert seen["args"] == (["m"], "session-1")
    assert seen["kwargs"] == {}

    # And by keyword, not only positionally. Review of #1791 caught that asserting
    # an empty kwargs dict does not exercise ``**kwargs`` forwarding at all: a
    # wrapper narrowed to ``*args`` alone still passes every assertion above, and
    # then raises the moment upstream passes anything by keyword.
    seen.clear()
    if is_async:
        assert asyncio.run(
            wrapped(None, "raw-backend", ["m"], session_id="session-kw")
        ) == "ok"
    else:
        assert wrapped(None, "raw-backend", ["m"], session_id="session-kw") == "ok"
    assert seen["args"] == (["m"],)
    assert seen["kwargs"] == {"session_id": "session-kw"}
    # ``backend`` itself is the one argument the wrapper is allowed to replace.
    assert seen["backend"] != "raw-backend"

    # The three-argument shape the inline-media siblings still use keeps working.
    seen.clear()
    if not is_async:
        three = _wrap_sync_offload(original, logging.getLogger(__name__))
        assert three(None, "raw-backend", ["m"]) == "ok"
        assert seen["args"] == (["m"],)
