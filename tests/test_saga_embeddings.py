"""Local embedding initialization is serialized, but inference is not."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from mimir.saga import embeddings


@pytest.fixture
def hub_stub(monkeypatch):
    class LocalEntryNotFoundError(Exception):
        pass

    hub = SimpleNamespace(
        snapshot_download=Mock(return_value="/cached/snapshots/5fb34e870f49f19e79c94d5e118f5ee730133279"),
        LocalEntryNotFoundError=LocalEntryNotFoundError,
    )
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    monkeypatch.setitem(sys.modules, "huggingface_hub.errors", hub)
    return hub


@pytest.fixture
def local_stub(monkeypatch, hub_stub):
    settings = {"model": "voyageai/voyage-4-nano", "dimensions": 1024}
    monkeypatch.setattr(embeddings, "_cfg", lambda section, key, default=None: settings.get(key, default))
    model = SimpleNamespace(prompts={"query": "q", "document": "d"},
                            encode_query=Mock(return_value=[3.0, 4.0]),
                            encode_document=Mock(return_value=[0.0, 2.0]),
                            encode=Mock(return_value=[1.0, 0.0]))
    constructor = Mock(return_value=model)
    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=constructor))
    return settings, model, constructor


def test_local_nano_lazy_pinned_prompt_routing_and_normalization(local_stub, hub_stub):
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
    hub_stub.snapshot_download.assert_called_once_with(
        "voyageai/voyage-4-nano",
        revision="5fb34e870f49f19e79c94d5e118f5ee730133279", local_files_only=True,
    )
    constructor.assert_called_once_with(
        hub_stub.snapshot_download.return_value, device="cpu", trust_remote_code=True,
    )
    settings["revision"] = "a" * 40
    hub_stub.snapshot_download.return_value = "/cached/snapshots/" + "a" * 40
    embeddings.LocalProvider().embed("override")
    assert hub_stub.snapshot_download.call_args.kwargs["revision"] == "a" * 40
    assert constructor.call_args.args == ("/cached/snapshots/" + "a" * 40,)


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


def test_non_allowlisted_model_cannot_load_remote_code_or_override_revision(local_stub, hub_stub):
    settings, model, constructor = local_stub
    settings.update(model="example/other", revision="a" * 40, dimensions=384, device="cuda")
    model.prompts = {}
    provider = embeddings.LocalProvider()
    assert provider.dimensions() == 384
    assert provider.embed("ordinary", input_type="query") == [1.0, 0.0]
    model.encode.assert_called_once_with("ordinary", truncate_dim=384)
    constructor.assert_called_once_with("example/other", device="cuda")
    hub_stub.snapshot_download.assert_not_called()


def test_local_load_uses_snapshot_path_and_reuses_success(local_stub, hub_stub):
    _, model, constructor = local_stub
    provider = embeddings.LocalProvider()
    assert provider._load_model() is model
    assert provider._load_model() is model
    hub_stub.snapshot_download.assert_called_once_with(
        provider.model_name, revision=embeddings.REMOTE_CODE_REVISIONS[provider.model_name],
        local_files_only=True,
    )
    constructor.assert_called_once_with(
        hub_stub.snapshot_download.return_value, device="cpu", trust_remote_code=True,
    )


@pytest.mark.parametrize("override", [None, "a" * 40])
def test_local_cache_miss_loads_hub_with_exact_revision(local_stub, hub_stub, override):
    settings, model, constructor = local_stub
    settings["revision"] = override
    hub_stub.snapshot_download.side_effect = hub_stub.LocalEntryNotFoundError("not cached")
    provider = embeddings.LocalProvider()
    assert provider._load_model() is model
    revision = override or embeddings.REMOTE_CODE_REVISIONS[provider.model_name]
    hub_stub.snapshot_download.assert_called_once_with(
        provider.model_name, revision=revision, local_files_only=True,
    )
    constructor.assert_called_once_with(
        provider.model_name, device="cpu", trust_remote_code=True, revision=revision,
    )


@pytest.mark.parametrize("stage", ["snapshot", "model"])
def test_local_real_load_errors_propagate_without_network_retry(local_stub, hub_stub, stage):
    _, _, constructor = local_stub
    failure = ValueError("real load failure")
    target = hub_stub.snapshot_download if stage == "snapshot" else constructor
    target.side_effect = failure
    provider = embeddings.LocalProvider()
    with pytest.raises(ValueError, match="real load failure") as caught:
        provider._load_model()
    assert caught.value is failure
    assert provider._model is None
    hub_stub.snapshot_download.assert_called_once()
    if stage == "snapshot":
        constructor.assert_not_called()
    else:
        constructor.assert_called_once_with(
            hub_stub.snapshot_download.return_value, device="cpu", trust_remote_code=True,
        )


def test_local_load_propagates_network_failure_without_caching(local_stub, hub_stub):
    _, model, constructor = local_stub
    hub_stub.snapshot_download.side_effect = hub_stub.LocalEntryNotFoundError("not cached")
    constructor.side_effect = [OSError("network down"), model]
    provider = embeddings.LocalProvider()
    with pytest.raises(OSError, match="network down"):
        provider._load_model()
    assert provider._model is None
    assert provider._load_model() is model
    assert constructor.call_args_list == [
        call(provider.model_name, device="cpu", trust_remote_code=True,
             revision=embeddings.REMOTE_CODE_REVISIONS[provider.model_name]),
    ] * 2


def test_missing_local_extra_names_install_command(monkeypatch):
    monkeypatch.setattr(embeddings, "_cfg", lambda section, key, default=None: default)
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    provider = embeddings.LocalProvider()
    with pytest.raises(RuntimeError, match=r"mimir-agent\[local-embeddings\]"):
        provider.embed("hello")


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
def provider_and_constructor(request, monkeypatch, hub_stub):
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
