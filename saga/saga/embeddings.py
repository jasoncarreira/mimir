"""
MSAM Embeddings -- Pluggable embedding provider interface.

Supports:
  - nvidia-nim: NVIDIA NIM API (nv-embedqa-e5-v5, etc.)
  - openai: OpenAI-compatible API (text-embedding-3-small, etc.)
  - onnx: ONNX Runtime local inference (bge-small-en-v1.5, no API needed)
  - local: sentence-transformers (no API key needed, runs on CPU/GPU)

Provider is configured via saga.toml [embedding] section.
"""

import logging
import os
import sys
import threading
import time
from functools import lru_cache
from pathlib import Path

from .config import get_config

_cfg = get_config()


def _retry_with_backoff(fn, max_retries=3, base_delay=1.0):
    """Retry a function with exponential backoff on transient HTTP errors."""
    import requests
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            result = fn()
            if hasattr(result, 'status_code') and result.status_code in (429, 500, 502, 503):
                raise requests.exceptions.HTTPError(f"HTTP {result.status_code}")
            return result
        except (requests.exceptions.RequestException, requests.exceptions.HTTPError) as e:
            last_err = e
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                print(f"Embedding retry {attempt+1}/{max_retries} after {delay:.1f}s: {e}", file=sys.stderr)
                time.sleep(delay)
    raise last_err


class EmbeddingProvider:
    """Base class for embedding providers."""

    def embed(self, text: str, input_type: str = "passage") -> list[float]:
        raise NotImplementedError

    def batch_embed(self, texts: list[str], input_type: str = "passage") -> list[list[float]]:
        """Batch embed. Default: sequential calls. Override for API batching."""
        return [self.embed(t, input_type) for t in texts]

    def dimensions(self) -> int:
        return _cfg('embedding', 'dimensions', 1024)


class NvidiaNimProvider(EmbeddingProvider):
    """NVIDIA NIM API provider (default)."""

    def __init__(self):
        self.url = _cfg('embedding', 'url', 'https://integrate.api.nvidia.com/v1/embeddings')
        self.model = _cfg('embedding', 'model', 'nvidia/nv-embedqa-e5-v5')
        self.timeout = _cfg('embedding', 'timeout_seconds', 10)
        self.max_chars = _cfg('embedding', 'max_input_chars', 2000)
        self.api_key_env = 'NVIDIA_NIM_API_KEY'

    def _call_api(self, inputs: list[str], input_type: str = "passage") -> list[list[float]]:
        import requests
        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise RuntimeError(f"{self.api_key_env} not set. Run: export {self.api_key_env}=\"your-key\" or switch to provider=\"onnx\" in saga.toml for local embeddings.")

        def _do_request():
            r = requests.post(
                self.url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"input": inputs, "model": self.model, "input_type": input_type},
                timeout=self.timeout,
            )
            r.raise_for_status()
            return r

        r = _retry_with_backoff(_do_request)
        data = r.json()["data"]
        return [d["embedding"] for d in sorted(data, key=lambda x: x["index"])]

    def embed(self, text: str, input_type: str = "passage") -> list[float]:
        return self._call_api([text[:self.max_chars]], input_type)[0]

    def batch_embed(self, texts: list[str], input_type: str = "passage") -> list[list[float]]:
        """Batch embed via NIM API."""
        results = []
        batch_size = _cfg('embedding', 'batch_size', 50)
        for i in range(0, len(texts), batch_size):
            chunk = [t[:self.max_chars] for t in texts[i:i + batch_size]]
            results.extend(self._call_api(chunk, input_type))
        return results


class OpenAIProvider(EmbeddingProvider):
    """OpenAI-compatible API provider (works with OpenAI, Azure, local vLLM, etc.)."""

    def __init__(self):
        self.url = _cfg('embedding', 'url', 'https://api.openai.com/v1/embeddings')
        self.model = _cfg('embedding', 'model', 'text-embedding-3-small')
        self.timeout = _cfg('embedding', 'timeout_seconds', 10)
        self.max_chars = _cfg('embedding', 'max_input_chars', 8000)
        self.api_key_env = _cfg('embedding', 'api_key_env', 'OPENAI_API_KEY')

    def embed(self, text: str, input_type: str = "passage") -> list[float]:
        return self._call_api([text[:self.max_chars]])[0]

    def _call_api(self, inputs: list[str]) -> list[list[float]]:
        import requests
        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise RuntimeError(f"{self.api_key_env} not set. Run: export {self.api_key_env}=\"your-key\" or switch to provider=\"onnx\" in saga.toml for local embeddings.")

        def _do_request():
            r = requests.post(
                self.url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"input": inputs, "model": self.model},
                timeout=self.timeout,
            )
            r.raise_for_status()
            return r

        r = _retry_with_backoff(_do_request)
        data = r.json()["data"]
        return [d["embedding"] for d in sorted(data, key=lambda x: x["index"])]

    def batch_embed(self, texts: list[str], input_type: str = "passage") -> list[list[float]]:
        results: list[list[float]] = []
        batch_size = _cfg('embedding', 'batch_size', 256)
        for i in range(0, len(texts), batch_size):
            chunk = [t[:self.max_chars] for t in texts[i:i + batch_size]]
            results.extend(self._call_api(chunk))
        return results


class LocalProvider(EmbeddingProvider):
    """Local sentence-transformers provider. No API key needed."""

    def __init__(self):
        self.model_name = _cfg('embedding', 'model', 'all-MiniLM-L6-v2')
        self.max_chars = _cfg('embedding', 'max_input_chars', 2000)
        self._model = None

    def _load_model(self):
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
                self._model = SentenceTransformer(self.model_name)
            except ImportError:
                raise RuntimeError(
                    "sentence-transformers not installed. "
                    "Run: pip install sentence-transformers"
                )
        return self._model

    def embed(self, text: str, input_type: str = "passage") -> list[float]:
        model = self._load_model()
        embedding = model.encode(text[:self.max_chars])
        return embedding.tolist()

    def dimensions(self) -> int:
        model = self._load_model()
        return model.get_sentence_embedding_dimension()


class ONNXProvider(EmbeddingProvider):
    """Local ONNX embedding provider, backed by fastembed.

    Default model: BAAI/bge-small-en-v1.5 (33MB ONNX, 384 dimensions).
    fastembed handles model fetch, ONNX runtime, tokenizer config, and
    pooling — same library mimir's file_search uses, so we share one
    on-disk cache (``~/.cache/fastembed/``) instead of paying for the
    model twice.

    Pre-fastembed implementation downloaded model.onnx + tokenizer.json
    by hand and ran ``onnxruntime.InferenceSession`` directly with a
    custom mean-pool. Functionally equivalent; this is just a runtime
    wrapper swap.
    """

    def __init__(self):
        self.model_name = _cfg('embedding', 'model', 'BAAI/bge-small-en-v1.5')
        self.max_chars = _cfg('embedding', 'max_input_chars', 2000)
        self._model = None

    def _load(self):
        if self._model is None:
            try:
                from fastembed import TextEmbedding
            except ImportError:
                raise RuntimeError(
                    "fastembed not installed. Run: pip install fastembed "
                    "(saga's pyproject already pulls it via the workspace; "
                    "this only fires in standalone-saga deployments that "
                    "skipped the optional embedding deps)."
                )
            self._model = TextEmbedding(model_name=self.model_name)
        return self._model

    def embed(self, text: str, input_type: str = "passage") -> list[float]:
        model = self._load()
        clipped = text[:self.max_chars]
        # BGE models use an instruction prefix for queries; fastembed
        # exposes both passage- and query-mode embedding via separate
        # entry points. Older fastembed versions only expose ``embed``;
        # fall back to that if ``query_embed`` is missing.
        if input_type == "query" and hasattr(model, "query_embed"):
            return list(model.query_embed([clipped]))[0].tolist()
        return list(model.embed([clipped]))[0].tolist()

    def batch_embed(self, texts: list[str],
                    input_type: str = "passage") -> list[list[float]]:
        """Override the default per-call loop with a real batch call —
        fastembed batches inside one ONNX run, materially faster on
        ingest than calling ``embed`` per text in Python."""
        model = self._load()
        clipped = [t[:self.max_chars] for t in texts]
        if input_type == "query" and hasattr(model, "query_embed"):
            vecs = model.query_embed(clipped)
        else:
            vecs = model.embed(clipped)
        return [v.tolist() for v in vecs]

    def dimensions(self) -> int:
        return _cfg('embedding', 'dimensions', 384)


# ─── Provider Registry ────────────────────────────────────────────

_PROVIDERS = {
    "nvidia-nim": NvidiaNimProvider,
    "openai": OpenAIProvider,
    "onnx": ONNXProvider,
    "local": LocalProvider,
}

_provider_instance = None
# CR#1: lock the singleton init. Saga is called from mimir's
# ``asyncio.to_thread`` workers — two concurrent first calls both saw
# ``_provider_instance is None`` and both constructed an
# ``ONNXProvider`` (which downloads/loads a 33MB model), with the loser
# leaking after warming the module-global ``cached_embed_query`` LRU
# against a concurrent re-init. Mirror ``saga/core.py``'s
# ``_migrations_lock`` double-checked-locking pattern.
_provider_lock = threading.Lock()


def get_provider() -> EmbeddingProvider:
    """Get the configured embedding provider (singleton).

    Auto-fallback: if the configured provider is API-keyed (``openai``,
    ``nvidia-nim``) and the required API key env var isn't set,
    transparently fall through to the ``onnx`` (fastembed) provider
    instead of raising on the first ``embed()`` call. Lets fresh
    ``mimir setup``-only installs work out of the box without an
    OpenAI key, while paid-tier users keep the better embeddings as
    long as their key is in the environment.
    """
    global _provider_instance
    # Double-checked locking: avoid acquiring on every call once the
    # singleton is initialized (the read of a single Python attribute
    # is atomic under the GIL — no torn read here).
    if _provider_instance is not None:
        return _provider_instance
    with _provider_lock:
        if _provider_instance is not None:
            return _provider_instance
        import os
        provider_name = _cfg('embedding', 'provider', 'nvidia-nim')

        if provider_name in ("openai", "nvidia-nim"):
            api_key_env = _cfg('embedding', 'api_key_env',
                               'NVIDIA_NIM_API_KEY' if provider_name == 'nvidia-nim' else 'OPENAI_API_KEY')
            if api_key_env and not os.environ.get(api_key_env):
                logging.getLogger("saga.embeddings").info(
                    "[embedding] provider=%s but %s is unset — falling back "
                    "to onnx (fastembed, BAAI/bge-small-en-v1.5). Set %s in "
                    "your environment to use %s.",
                    provider_name, api_key_env, api_key_env, provider_name,
                )
                provider_name = "onnx"

        provider_cls = _PROVIDERS.get(provider_name)
        if provider_cls is None:
            raise ValueError(
                f"Unknown embedding provider: {provider_name}. "
                f"Available: {', '.join(_PROVIDERS.keys())}"
            )
        _provider_instance = provider_cls()
    return _provider_instance


def embed_text(text: str) -> list[float]:
    """Embed text for storage (passage mode)."""
    start = time.time()
    success = True
    max_chars = _cfg('embedding', 'max_input_chars', 2000)
    try:
        result = get_provider().embed(text[:max_chars], input_type="passage")
        return result
    except Exception:
        success = False
        raise
    finally:
        latency_ms = (time.time() - start) * 1000
        try:
            from .metrics import log_embedding
            log_embedding('embed_text', latency_ms, len(text[:max_chars]), success)
        except Exception:
            pass


def embed_query(text: str) -> list[float]:
    """Embed text for retrieval (query mode)."""
    start = time.time()
    success = True
    max_chars = _cfg('embedding', 'max_input_chars', 2000)
    try:
        result = get_provider().embed(text[:max_chars], input_type="query")
        return result
    except Exception:
        success = False
        raise
    finally:
        latency_ms = (time.time() - start) * 1000
        try:
            from .metrics import log_embedding
            log_embedding('embed_query', latency_ms, len(text[:max_chars]), success)
        except Exception:
            pass


def batch_embed_texts(texts: list[str]) -> list[list[float]]:
    """Batch embed texts for storage (passage mode)."""
    start = time.time()
    max_chars = _cfg('embedding', 'max_input_chars', 2000)
    truncated = [t[:max_chars] for t in texts]
    result = get_provider().batch_embed(truncated, input_type="passage")
    latency_ms = (time.time() - start) * 1000
    try:
        from .metrics import log_embedding
        log_embedding('batch_embed', latency_ms, sum(len(t) for t in truncated), True)
    except Exception:
        pass
    return result


@lru_cache(maxsize=64)
def cached_embed_query(text: str) -> tuple:
    """Cached query embedding. Returns tuple (hashable for LRU cache)."""
    return tuple(embed_query(text))
