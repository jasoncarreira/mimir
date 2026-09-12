from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path

import pytest

from mimir.agent import Agent


@pytest.fixture
def agent():
    agent = Agent.__new__(Agent)
    agent._skill_fingerprint_executor = None
    agent._skill_fingerprint_cache = None
    yield agent
    if agent._skill_fingerprint_executor is not None:
        agent._skill_fingerprint_executor.shutdown(wait=True)


@pytest.mark.parametrize("change", ["add", "delete", "nested_edit", "restored_mtime", "replace"])
async def test_next_call_invalidates(agent, tmp_path, change):
    skill = tmp_path / "nested" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("original")
    sources = [str(tmp_path)]
    before = await agent._skill_catalog_fingerprint(sources)
    stat = skill.stat()
    if change == "add":
        added = tmp_path / "nested" / "new" / "SKILL.md"
        added.parent.mkdir()
        added.write_text("new skill")
    elif change == "delete":
        skill.unlink()
    elif change == "replace":
        replacement = skill.with_name("replacement")
        replacement.write_text("modified")
        os.utime(replacement, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        replacement.replace(skill)
    else:
        skill.write_text("modified")  # Same size, including the restored-mtime case.
        if change == "restored_mtime":
            os.utime(skill, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            assert skill.stat().st_mtime_ns == stat.st_mtime_ns
            assert skill.stat().st_ctime_ns != stat.st_ctime_ns
    after = await agent._skill_catalog_fingerprint(sources)
    assert before != after
    assert await agent._skill_catalog_fingerprint(sources) == after


async def test_source_discovery_removal_and_order(agent, tmp_path):
    sources = [str(tmp_path / "builtin"), str(tmp_path / "operator")]
    missing = await agent._skill_catalog_fingerprint(sources)
    root = Path(sources[0])
    root.mkdir()
    (root / "SKILL.md").write_text("skill")
    discovered = await agent._skill_catalog_fingerprint(sources)
    assert discovered != missing
    assert await agent._skill_catalog_fingerprint(sources[::-1]) != discovered
    (root / "SKILL.md").unlink()
    root.rmdir()
    assert await agent._skill_catalog_fingerprint(sources) == missing


async def test_unchanged_skips_content_reads(agent, tmp_path, monkeypatch):
    skill = tmp_path / "SKILL.md"
    skill.write_text("skill")
    sources = [str(tmp_path)]
    expected = await agent._skill_catalog_fingerprint(sources)
    original = Path.read_bytes

    def read(path):
        assert path != skill, "unchanged skill content was read"
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    assert await agent._skill_catalog_fingerprint(sources) == expected


async def test_edit_during_hash_does_not_cache_stale_content(agent, tmp_path, monkeypatch):
    skill = tmp_path / "SKILL.md"
    skill.write_text("original")
    original = Path.read_bytes
    reads = []

    def read(path):
        data = original(path)
        if path == skill:
            reads.append(data)
            if len(reads) == 1:
                path.write_text("modified")
        return data

    monkeypatch.setattr(Path, "read_bytes", read)
    sources = [str(tmp_path)]
    stale = await agent._skill_catalog_fingerprint(sources)
    assert agent._skill_fingerprint_cache is None
    fresh = await agent._skill_catalog_fingerprint(sources)
    assert fresh != stale
    assert await agent._skill_catalog_fingerprint(sources) == fresh
    assert reads == [b"original", b"modified"]


@pytest.mark.parametrize("operation", ["read_bytes", "stat"])
async def test_unreadable_hash_is_not_cached(agent, tmp_path, monkeypatch, operation):
    skill = tmp_path / "SKILL.md"
    skill.write_text("skill")
    original = getattr(Path, operation)

    def unreadable(path, *args, **kwargs):
        if path == skill:
            raise PermissionError("temporary read failure")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        if operation == "stat":
            # Model discovery succeeding before the subsequent stat fails.
            patch.setattr(Path, "rglob", lambda *args: iter([skill]))
        patch.setattr(Path, operation, unreadable)
        incomplete = await agent._skill_catalog_fingerprint([str(tmp_path)])
    assert agent._skill_fingerprint_cache is None
    complete = await agent._skill_catalog_fingerprint([str(tmp_path)])
    assert (complete != incomplete) == (operation == "read_bytes")


@pytest.mark.parametrize("operation", ["scan", "hash"])
async def test_io_is_off_loop_and_serialized_after_cancellation(
    agent, tmp_path, monkeypatch, operation,
):
    skill = tmp_path / "SKILL.md"
    skill.write_text("skill")
    sources = [str(tmp_path)]
    if operation == "scan":
        await agent._skill_catalog_fingerprint(sources)
    entered = threading.Event()
    release = threading.Event()
    loop_thread = threading.get_ident()
    workers = []
    method = "rglob" if operation == "scan" else "read_bytes"
    original = getattr(Path, method)

    def blocked(path, *args, **kwargs):
        if path == (tmp_path if operation == "scan" else skill):
            workers.append(threading.get_ident())
            assert workers[-1] != loop_thread
            entered.set()
            assert release.wait(5), "event loop failed to release filesystem worker"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, method, blocked)
    first = asyncio.create_task(agent._skill_catalog_fingerprint(sources))
    second = None
    try:
        async with asyncio.timeout(3):
            while not entered.is_set():
                await asyncio.sleep(0.001)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(agent._skill_catalog_fingerprint(sources))
        # The loop advances while this component's filesystem call is blocked.
        for _ in range(10):
            await asyncio.sleep(0.001)
        assert not second.done()
        assert len(workers) == 1
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
    assert second is not None
    assert second.result()
    assert len(set(workers)) == 1
