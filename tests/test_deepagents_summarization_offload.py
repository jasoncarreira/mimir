from __future__ import annotations

import logging
from pathlib import Path
from types import MethodType, SimpleNamespace

from deepagents.middleware.summarization import SummarizationMiddleware
from langchain_core.messages import HumanMessage

from mimir._deepagents_summarization import install_offload_traceback_logging_patch
from mimir._context import reset_current_turn, set_current_turn
from mimir.config import Config
from mimir.models import AuthContext
from mimir.readonly_backend import WriteGuardBackend


def _middleware(path: str) -> SummarizationMiddleware:
    middleware = object.__new__(SummarizationMiddleware)
    middleware._get_history_path = MethodType(lambda _self: path, middleware)
    middleware._filter_summary_messages = MethodType(
        lambda _self, messages: messages, middleware
    )
    return middleware


def test_production_backend_offloads_and_appends_on_fresh_home(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.delenv("MIMIR_FOLDERS", raising=False)
    config = Config.from_env()
    backend = WriteGuardBackend(
        root_dir=config.home,
        writable_dirs=config.writable_dirs,
        guard_outside_root=True,
    )
    middleware = _middleware("/conversation_history/thread-973.md")
    auth = AuthContext(
        principal="user",
        canonical_principal="user",
        roles=("user",),
        event_ingress=None,
        trigger="user_message",
        channel_id="channel",
        interactivity=None,
        enforcement_enabled=True,
    )

    token = set_current_turn(SimpleNamespace(turn_id="offload", auth_context=auth))
    try:
        first = middleware._offload_to_backend(backend, [HumanMessage(content="first")])
        second = middleware._offload_to_backend(backend, [HumanMessage(content="second")])
        direct_read = backend.read("/conversation_history/thread-973.md")
    finally:
        reset_current_turn(token)

    history = tmp_path / "conversation_history" / "thread-973.md"
    assert first == second == "/conversation_history/thread-973.md"
    assert history.is_file()
    assert history.read_text().count("## Summarized at ") == 2
    assert "Human: first" in history.read_text()
    assert "Human: second" in history.read_text()
    assert direct_read.error == "Read denied: protected file"

    denied = backend.write("/not_writable/outside.md", "blocked")
    assert "Write blocked" in (denied.error or "")
    assert not (tmp_path / "not_writable" / "outside.md").exists()


def test_offload_exception_is_logged_with_traceback(caplog) -> None:
    install_offload_traceback_logging_patch()
    middleware = _middleware("/conversation_history/thread-error.md")

    class FailingBackend:
        def download_files(self, _paths):
            return []

        def write(self, _path, _content):
            raise RuntimeError("disk unavailable")

    with caplog.at_level(logging.WARNING, logger="deepagents.middleware.summarization"):
        result = middleware._offload_to_backend(FailingBackend(), [HumanMessage(content="x")])

    assert result is None
    record = next(record for record in caplog.records if "Exception offloading" in record.message)
    assert record.exc_info is not None
    assert record.exc_info[0] is RuntimeError
    assert "disk unavailable" in caplog.text
