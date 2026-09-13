"""Local embedding initialization is serialized, but inference is not."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mimir.saga import embeddings


@pytest.fixture(params=["onnx", "local"])
def provider_and_constructor(request, monkeypatch):
    monkeypatch.setattr(embeddings, "_cfg", lambda section, key, default=None: default)
    vector = SimpleNamespace(tolist=lambda: [0.1, 0.2])
    model = SimpleNamespace(
        embed=lambda texts: iter([vector for _ in texts]),
        encode=lambda text: vector,
    )
    constructor = Mock(return_value=model)
    if request.param == "onnx":
        monkeypatch.setitem(sys.modules, "fastembed", SimpleNamespace(TextEmbedding=constructor))
        provider = embeddings.ONNXProvider()
    else:
        monkeypatch.setitem(
            sys.modules, "sentence_transformers",
            SimpleNamespace(SentenceTransformer=constructor),
        )
        provider = embeddings.LocalProvider()
    return provider, constructor


async def test_four_concurrent_embed_probes_load_once(provider_and_constructor):
    provider, constructor = provider_and_constructor
    barrier = threading.Barrier(4, timeout=10)
    lock = provider._load_lock

    class ContendedLock:
        def __enter__(self):
            # All four callers must observe the unloaded model before locking.
            barrier.wait()
            lock.acquire()

        def __exit__(self, *exc):
            lock.release()

    provider._load_lock = ContendedLock()
    results = await asyncio.gather(*(
        asyncio.to_thread(provider.embed, "probe") for _ in range(4)
    ))

    assert results == [[0.1, 0.2]] * 4
    constructor.assert_called_once()
    assert provider._model is constructor.return_value
    # A warm call must bypass the load lock (and its four-party barrier).
    assert provider.embed("warm") == [0.1, 0.2]


async def test_inference_runs_concurrently(provider_and_constructor):
    provider, constructor = provider_and_constructor
    assert provider.embed("warm") == [0.1, 0.2]
    barrier = threading.Barrier(4, timeout=10)
    vector = SimpleNamespace(tolist=lambda: [0.3, 0.4])

    def encode(text):
        barrier.wait()
        return vector

    def embed(texts):
        # Fastembed performs inference while consuming the iterator.
        barrier.wait()
        yield vector

    constructor.return_value.encode = encode
    constructor.return_value.embed = embed
    results = await asyncio.gather(*(
        asyncio.to_thread(provider.embed, "probe") for _ in range(4)
    ))

    assert results == [[0.3, 0.4]] * 4
    constructor.assert_called_once()


def test_load_locks_are_per_instance(provider_and_constructor):
    provider, _ = provider_and_constructor
    assert provider._load_lock is not type(provider)()._load_lock


@pytest.mark.parametrize("configured", [False, True])
def test_native_thread_defaults_at_import(configured):
    env = os.environ.copy()
    for name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        env.pop(name, None)
    if configured:
        env.update(OMP_NUM_THREADS="2", MKL_NUM_THREADS="3")
    result = subprocess.run(
        [sys.executable, "-c", """
import os
expected = {name: os.environ.get(name, str(os.cpu_count() or 1))
            for name in ('OMP_NUM_THREADS', 'MKL_NUM_THREADS')}
from mimir.saga import embeddings
for name, value in expected.items():
    assert os.environ[name] == value
"""],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
