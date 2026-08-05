"""Regression: the runtime must wire the IndexGenerator into the
``rebuild_index`` tool, or the tool is dead.

The composition root owns both the human-readable IndexGenerator and the search
Indexer. The wiring is guarded by source inspection and the tool behavior is
covered directly.
"""

from __future__ import annotations

import inspect

import pytest

from mimir.tools.extra import _INDEX_GEN_STATE, rebuild_index, set_index_generator


def test_runtime_wires_the_index_generator() -> None:
    from mimir.runtime import _install_runtime_globals

    src = inspect.getsource(_install_runtime_globals)
    assert "set_index_generator(bundle.indexes)" in src, (
        "the runtime must install its IndexGenerator next to its Indexer — "
        "without it the rebuild_index tool is dead "
        '("no IndexGenerator configured").'
    )


class _StubGen:
    def __init__(self) -> None:
        self.dirtied: list[str] = []
        self.flushed = False

    def mark_dirty(self, scope: str) -> None:
        self.dirtied.append(scope)

    async def flush(self) -> None:
        self.flushed = True


@pytest.fixture(autouse=True)
def _reset_generator():
    # Module-global, process-scoped — reset around each test so leakage
    # between tests (and from a real build_app elsewhere) can't mask a regression.
    saved = _INDEX_GEN_STATE["generator"]
    _INDEX_GEN_STATE["generator"] = None
    yield
    _INDEX_GEN_STATE["generator"] = saved


@pytest.mark.asyncio
async def test_rebuild_index_dead_without_generator() -> None:
    _INDEX_GEN_STATE["generator"] = None
    out = await rebuild_index.ainvoke({"scope": "all"})
    assert "no IndexGenerator configured" in out


@pytest.mark.asyncio
async def test_rebuild_index_works_once_wired() -> None:
    stub = _StubGen()
    set_index_generator(stub)
    out = await rebuild_index.ainvoke({"scope": "memory"})
    assert out == "rebuild_index ok: scope=memory"
    assert stub.dirtied == ["memory"]
    assert stub.flushed is True
