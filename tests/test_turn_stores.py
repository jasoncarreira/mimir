"""Turn-owned offloads and upgrade cleanup must not leak into later turns."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from langchain_core.messages import HumanMessage, ToolMessage

from mimir._context import reset_current_turn, set_current_turn
from mimir.access_control import ensure_turn_scratch
from mimir._deepagents_summarization import install_offload_traceback_logging_patch
from mimir.readonly_backend import MimirFilesystemMiddleware, WriteGuardBackend
from mimir.turn_stores import (
    move_ci_logs_to_turn, seed_home_ignore, sweep_turn_stores, wipe_ci_logs,
    wipe_turn_stores,
)
from mimir.tools import web


@pytest.mark.asyncio
async def test_wipe_real_writers_only_removes_own_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deepagents.middleware.summarization import SummarizationMiddleware

    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    backend = WriteGuardBackend(root_dir=tmp_path, writable_dirs=["conversation_history"])
    filesystem = MimirFilesystemMiddleware(backend=backend, tool_token_limit_before_evict=1)
    install_offload_traceback_logging_patch()
    history = SummarizationMiddleware(model="openai:test", backend=backend)
    web.set_home(tmp_path)
    monkeypatch.setattr(web, "_validate_fetch_url", lambda url: None)

    def download(*, target_path: Path, **_kwargs: object) -> dict:
        body = b"fetched page"
        target_path.write_bytes(body)
        return {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body),
                "status": 200, "content_type": "text/plain"}

    monkeypatch.setattr(web, "_download_url_bytes", download)
    written: list[list[Path]] = []
    try:
        for turn_id in ("turn-A", "turn-B"):
            scratch = ensure_turn_scratch(tmp_path, turn_id)
            token = set_current_turn(SimpleNamespace(
                turn_id=turn_id, turn_scratch_path=scratch, evicted_tool_result_ids=[],
            ))
            try:
                processed, evicted = filesystem._process_large_message(
                    ToolMessage(content="offloaded content " * 10, tool_call_id=turn_id), backend,
                )
                assert evicted and f"/{turn_id}/{turn_id}" in str(processed.content)
                pointer = history._offload_to_backend(backend, [HumanMessage(content="summary bytes")], "session")
                assert pointer and f"/{turn_id}/session.md" in pointer
                fetched = yaml.safe_load(await web.fetch_url.ainvoke({"url": "https://example.com/a"}))
                assert f"/{turn_id}/" in fetched["file_path"]
                paths = [tmp_path / "large_tool_results" / turn_id / turn_id,
                         tmp_path / pointer.lstrip("/"), tmp_path / fetched["file_path"].lstrip("/")]
                assert all(path.is_file() for path in paths)
                written.append(paths)
                if turn_id == "turn-B":
                    # A and B's stores coexist while B is still live.
                    wipe_turn_stores(tmp_path, "turn-A")
                    assert all(not path.exists() for path in written[0])
                    assert all(path.is_file() for path in written[1])
            finally:
                reset_current_turn(token)
        assert all(path.is_file() for path in written[1])
        wipe_turn_stores(tmp_path, "turn-B")
        assert all(not path.exists() for path in written[1])
    finally:
        web.set_home(None)


def test_rollout_purge_is_one_time_and_restart_sweeps_crashed_turns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    stores = ["large_tool_results", "conversation_history", ".mimir/conversation_history_labels",
              "attachments/fetch-cache"]
    for store in stores:
        root = tmp_path / store
        root.mkdir(parents=True)
        (root / "legacy.md").write_text("old untrusted bytes")
    outside = tmp_path / "outside.md"
    outside.write_text("safe")
    (tmp_path / "large_tool_results" / "link").symlink_to(outside)
    external = tmp_path / "external"
    external.mkdir()
    (external / "body").write_text("untouched")
    (tmp_path / "conversation_history" / "linked-dir").symlink_to(external, target_is_directory=True)
    with caplog.at_level(logging.INFO):
        sweep_turn_stores(tmp_path)
        assert all(not (tmp_path / store / "legacy.md").exists() for store in stores)
        assert outside.read_text() == "safe"
        assert (external / "body").read_text() == "untouched"
        (tmp_path / "large_tool_results" / "crashed").mkdir()
        (tmp_path / "large_tool_results" / "crashed" / "body").write_text("stale")
        sweep_turn_stores(tmp_path)
    assert not (tmp_path / "large_tool_results" / "crashed").exists()
    events = [entry for entry in caplog.records if "turn_store_rollout_purge" in entry.message]
    assert len(events) == 1
    assert "old untrusted bytes" not in events[0].message
    assert all(f"'{store}': 1" in events[0].message for store in stores)


def test_rollout_refuses_symlinked_store_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    body = outside / "keep.txt"
    body.write_text("safe")
    (tmp_path / "large_tool_results").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        sweep_turn_stores(tmp_path)
    assert body.read_text() == "safe"


def test_home_ignore_seed_and_upgrade(tmp_path: Path) -> None:
    (tmp_path / ".ignore").write_text("custom/\nlogs/\n")
    seed_home_ignore(tmp_path)
    seed_home_ignore(tmp_path)
    content = (tmp_path / ".ignore").read_text().splitlines()
    assert content.count("logs/") == 1 and "custom/" in content
    assert set(content) >= {"logs/", "messages/", "large_tool_results/",
                            "conversation_history/", "attachments/fetch-cache/", "state/pollers/"}


def test_ci_log_delivery_moves_to_consumer_then_deletes(tmp_path: Path) -> None:
    source = tmp_path / "state/pollers/github-ci-watch/logs/42-101.log"
    source.parent.mkdir(parents=True)
    source.write_text("untrusted job output")
    scratch = ensure_turn_scratch(tmp_path, "consumer")
    event = SimpleNamespace(service_principal="poller:github-ci-watch",
                            content=f"Read the bounded log tail using read_file: {source}")
    moved = move_ci_logs_to_turn(tmp_path, event, scratch)
    assert len(moved) == 1 and not source.exists()
    assert moved[0].read_text() == "untrusted job output"
    assert str(moved[0]) in event.content
    wipe_ci_logs(moved)
    assert not moved[0].exists()


def test_research_draft_last_writer_owns_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir import access_control
    from mimir.turn_stores import wipe_research_drafts

    service = SimpleNamespace(canonical="poller:papers")
    monkeypatch.setattr(access_control, "get_trusted_service_from_auth_context", lambda auth: service)
    monkeypatch.setattr(access_control, "_is_research_proposal_poller", lambda candidate: candidate is service)
    backend = WriteGuardBackend(root_dir=tmp_path, writable_dirs=["state"])
    path = tmp_path / "state/pollers/papers/drafts/paper.md"
    for turn_id in ("A", "B"):
        token = set_current_turn(SimpleNamespace(turn_id=turn_id, auth_context=object()))
        try:
            result = (
                backend.write("/state/pollers/papers/drafts/paper.md", turn_id)
                if turn_id == "A" else
                backend.replace("/state/pollers/papers/drafts/paper.md", turn_id, allow_shrink=True)
            )
            assert result.error is None
        finally:
            reset_current_turn(token)
    wipe_research_drafts(tmp_path, "A")
    assert path.read_text() == "B"
    wipe_research_drafts(tmp_path, "B")
    assert not path.exists()


def test_refused_turn_store_never_falls_back_to_shared_fetch_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    scratch = ensure_turn_scratch(tmp_path, "refused")
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "attachments").mkdir()
    (tmp_path / "attachments" / "fetch-cache").symlink_to(outside, target_is_directory=True)
    web.set_home(tmp_path)
    token = set_current_turn(SimpleNamespace(turn_id="refused", turn_scratch_path=scratch))
    try:
        with pytest.raises(RuntimeError, match="valid turn scratch"):
            web._fetch_cache_dir()
        assert not list(outside.iterdir())
    finally:
        reset_current_turn(token)
        web.set_home(None)
