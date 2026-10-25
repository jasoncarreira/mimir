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


@pytest.fixture
def local_stub(monkeypatch):
    settings = {"model": "voyageai/voyage-4-nano", "dimensions": 1024}
    monkeypatch.setattr(embeddings, "_cfg", lambda section, key, default=None: settings.get(key, default))
    model = SimpleNamespace(prompts={"query": "q", "document": "d"},
                            encode_query=Mock(return_value=[3.0, 4.0]),
                            encode_document=Mock(return_value=[0.0, 2.0]),
                            encode=Mock(return_value=[1.0, 0.0]))
    constructor = Mock(return_value=model)
    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=constructor))
    return settings, model, constructor


def test_local_nano_lazy_pinned_prompt_routing_and_normalization(local_stub):
    settings, model, constructor = local_stub
    provider = embeddings.LocalProvider()
    assert provider.dimensions() == 1024
    constructor.assert_not_called()
    assert provider.embed("a query", input_type="query") == pytest.approx([0.6, 0.8])
    model.encode_query.assert_called_once_with("a query", truncate_dim=1024)
    assert provider.embed("a document", input_type="passage") == [0.0, 1.0]
    model.encode_document.assert_called_once_with("a document", truncate_dim=1024)
    assert provider.embed("other", input_type="anything") == [0.0, 1.0]
    model.encode.assert_not_called()
    constructor.assert_called_once_with(
        "voyageai/voyage-4-nano", device="cpu", trust_remote_code=True,
        revision="5fb34e870f49f19e79c94d5e118f5ee730133279",
    )
    settings["revision"] = "a" * 40
    embeddings.LocalProvider().embed("override")
    assert constructor.call_args.kwargs["revision"] == "a" * 40


@pytest.mark.parametrize("dim", [2048, 1024, 512, 256])
def test_nano_accepts_mrl_dimensions_without_loading(local_stub, dim):
    settings, _, constructor = local_stub
    settings["dimensions"] = dim
    assert embeddings.LocalProvider().dimensions() == dim
    constructor.assert_not_called()


@pytest.mark.parametrize("dim", [384, 1536, 0, "1024", True])
def test_nano_rejects_unsupported_dimensions_at_construction(local_stub, dim):
    settings, _, constructor = local_stub
    settings["dimensions"] = dim
    with pytest.raises(ValueError, match="voyageai/voyage-4-nano.*2048, 1024, 512 or 256"):
        embeddings.LocalProvider()
    constructor.assert_not_called()


def test_non_allowlisted_model_cannot_load_remote_code_or_override_revision(local_stub):
    settings, model, constructor = local_stub
    settings.update(model="example/other", revision="a" * 40, dimensions=384, device="cuda")
    model.prompts = {}
    provider = embeddings.LocalProvider()
    assert provider.dimensions() == 384
    assert provider.embed("ordinary", input_type="query") == [1.0, 0.0]
    model.encode.assert_called_once_with("ordinary", truncate_dim=384)
    constructor.assert_called_once_with("example/other", device="cuda")


def test_missing_local_extra_names_install_command(monkeypatch):
    monkeypatch.setattr(embeddings, "_cfg", lambda section, key, default=None: default)
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    provider = embeddings.LocalProvider()
    with pytest.raises(RuntimeError, match=r"mimir-agent\[local-embeddings\]"):
        provider.embed("hello")


@pytest.fixture(params=["onnx", "local"])
def provider_and_constructor(request, monkeypatch):
    monkeypatch.setattr(embeddings, "_cfg", lambda section, key, default=None: default)
    vector = SimpleNamespace(tolist=lambda: [0.6, 0.8])
    model = SimpleNamespace(
        embed=lambda texts: iter([vector for _ in texts]),
        encode=lambda text, **kwargs: vector,
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

    assert results == [[0.6, 0.8]] * 4
    constructor.assert_called_once()
    assert provider._model is constructor.return_value
    # A warm call must bypass the load lock (and its four-party barrier).
    assert provider.embed("warm") == [0.6, 0.8]


async def test_inference_runs_concurrently(provider_and_constructor):
    provider, constructor = provider_and_constructor
    assert provider.embed("warm") == [0.6, 0.8]
    barrier = threading.Barrier(4, timeout=10)
    vector = SimpleNamespace(tolist=lambda: [0.8, 0.6])

    def encode(text, **kwargs):
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

    assert results == [[0.8, 0.6]] * 4
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
