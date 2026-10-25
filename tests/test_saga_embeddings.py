"""Local embedding initialization is serialized, but inference is not."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mimir.saga import embeddings


@pytest.fixture
def recorded_onnx(monkeypatch):
    calls = []

    def text_embedding(**kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setitem(sys.modules, "fastembed", SimpleNamespace(TextEmbedding=text_embedding))
    return calls


@pytest.mark.parametrize("fastembed,xdg,expected", [
    ("custom/onnx", "xdg", "custom/onnx"),
    (None, "xdg", "xdg/fastembed"),
    (None, None, "home/.cache/fastembed"),
])
def test_onnx_cache_resolution_is_lazy(tmp_path, monkeypatch, recorded_onnx,
                                       fastembed, xdg, expected):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for key, value in (("FASTEMBED_CACHE_PATH", fastembed), ("XDG_CACHE_HOME", xdg)):
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, str(tmp_path / value))
    cache = tmp_path / expected
    provider = embeddings.ONNXProvider(model_name="BAAI/bge-small-en-v1.5")
    assert not cache.exists()
    assert not recorded_onnx

    provider._load()
    assert cache.is_dir()
    assert recorded_onnx == [{"model_name": "BAAI/bge-small-en-v1.5",
                              "cache_dir": str(cache)}]
    assert provider._load() is provider._model
    assert len(recorded_onnx) == 1


@pytest.mark.parametrize("failure", ["mkdir", "write"])
def test_onnx_unwritable_cache_warns_once_and_uses_temp(tmp_path, monkeypatch,
                                                         recorded_onnx, caplog, failure):
    cache = tmp_path / "persistent" / "fastembed"
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(cache))
    monkeypatch.setattr(embeddings.tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    if failure == "mkdir":
        original_mkdir = Path.mkdir

        def mkdir(path, *args, **kwargs):
            if path == cache:
                raise PermissionError("read-only volume")
            return original_mkdir(path, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", mkdir)
    else:
        original_probe = embeddings.tempfile.TemporaryFile

        def probe(*args, **kwargs):
            if kwargs.get("dir") == cache:
                raise PermissionError("read-only existing cache")
            return original_probe(*args, **kwargs)

        monkeypatch.setattr(embeddings.tempfile, "TemporaryFile", probe)

    provider = embeddings.ONNXProvider()
    with caplog.at_level("WARNING", logger=embeddings.__name__):
        provider._load()
        provider._load()
    assert recorded_onnx == [{"model_name": provider.model_name,
                              "cache_dir": str(tmp_path / "tmp" / "fastembed_cache")}]
    assert len([record for record in caplog.records if "unwritable" in record.message]) == 1


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
