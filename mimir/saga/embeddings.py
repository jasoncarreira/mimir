"""
SAGA Embeddings -- Pluggable embedding provider interface.

Supports:
  - voyage: Voyage AI API (voyage-4-lite, etc.) — the default
  - openai: OpenAI-compatible API (text-embedding-3-small, etc.)
  - onnx: ONNX Runtime local inference (bge-small-en-v1.5, no API needed)
  - local: sentence-transformers (no API key needed, runs on CPU/GPU)

When the configured provider is API-keyed (voyage/openai) and its API
key env var is unset, ``get_provider()`` auto-falls back to ``onnx`` so
a keyless install still works.

Provider is configured via saga.toml [embedding] section.
"""

import logging
import os
import threading
import time
from functools import lru_cache
from typing import Optional

from ._config_io import get_config

log = logging.getLogger(__name__)

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
                log.warning(
                    "embedding retry %d/%d after %.1fs: %s",
                    attempt + 1, max_retries, delay, e,
                )
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


class OpenAIProvider(EmbeddingProvider):
    """OpenAI-compatible API provider (works with OpenAI, Azure, local vLLM,
    and — with ``send_input_type=true`` — Voyage AI).

    ``send_input_type`` config flag (default False): when True, includes
    the ``input_type`` parameter ("query" / "document") in the JSON
    request body. Voyage AI REQUIRES this for retrieval-quality
    embeddings (model trained expecting instruction prefixes); OpenAI's
    API REJECTS the parameter as unknown. The flag must be set per
    provider deployment via ``[embedding] send_input_type = true`` in
    saga.toml.

    The flag also maps saga's internal ``"passage"`` → ``"document"`` to
    match Voyage's accepted vocabulary. Voyage's two valid values are
    ``"query"`` and ``"document"``; saga internally uses
    ``"query"`` / ``"passage"`` (a historical holdover). The mapping is
    one-way: ``passage → document``, ``query → query``.
    """

    def __init__(self):
        self.url = _cfg('embedding', 'url', 'https://api.openai.com/v1/embeddings')
        self.model = _cfg('embedding', 'model', 'text-embedding-3-small')
        self.timeout = _cfg('embedding', 'timeout_seconds', 10)
        self.max_chars = _cfg('embedding', 'max_input_chars', 8000)
        self.api_key_env = _cfg('embedding', 'api_key_env', 'OPENAI_API_KEY')
        # Default False — sending input_type to OpenAI's embeddings API
        # results in a 400 "Unknown parameter" error. Operators pointing
        # this provider at Voyage flip this to True.
        self.send_input_type = _cfg('embedding', 'send_input_type', False)

    def embed(self, text: str, input_type: str = "passage") -> list[float]:
        return self._call_api([text[:self.max_chars]], input_type)[0]

    def _call_api(
        self, inputs: list[str], input_type: str = "passage",
    ) -> list[list[float]]:
        import requests
        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise RuntimeError(f"{self.api_key_env} not set. Run: export {self.api_key_env}=\"your-key\" or switch to provider=\"onnx\" in saga.toml for local embeddings.")

        payload: dict = {"input": inputs, "model": self.model}
        if self.send_input_type:
            # Voyage uses "document" where saga internally says "passage".
            voyage_input_type = "document" if input_type == "passage" else input_type
            payload["input_type"] = voyage_input_type

        def _do_request():
            r = requests.post(
                self.url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
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
            results.extend(self._call_api(chunk, input_type))
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

    def __init__(self, model_name: Optional[str] = None):
        # Override for the API-key-fallback path: the configured model
        # is typically an API model name that fastembed rejects.
        self.model_name = (
            model_name
            if model_name is not None
            else _cfg('embedding', 'model', 'BAAI/bge-small-en-v1.5')
        )
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
            # Explicit thread count avoids ORT's affinity syscall,
            # which fails in some container runtimes.
            n = str(os.cpu_count() or 1)
            os.environ.setdefault("OMP_NUM_THREADS", n)
            os.environ.setdefault("MKL_NUM_THREADS", n)
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


class VoyageProvider(OpenAIProvider):
    """Voyage AI (acquired by MongoDB Feb 2025) embeddings via their
    OpenAI-compatible REST API.

    Wraps OpenAIProvider with the voyage-specific defaults baked in:

    - ``url``: ``https://api.voyageai.com/v1/embeddings``
    - ``model``: ``voyage-4-lite`` (default — best price/quality at
      $0.02/1M tokens with 200M signup free credit)
    - ``api_key_env``: ``VOYAGE_API_KEY``
    - ``send_input_type``: ``True`` (REQUIRED — voyage models trained
      with instruction prefixes that don't get applied without it)

    Operators can still override any of the above via the ``[embedding]``
    block in saga.toml. The defaults exist so a minimal config
    (``provider = "voyage"`` alone) just works.
    """

    def __init__(self):
        # Reuse OpenAIProvider's __init__ via super(), then patch the
        # voyage-specific defaults on top of whatever it read from
        # config. We can't pass the defaults as constructor args
        # because the base class reads them from _cfg directly; just
        # post-override.
        super().__init__()
        # Apply voyage defaults only for keys the operator didn't
        # explicitly set in saga.toml. We check ``was_set_in_toml``
        # rather than comparing against a default URL: the base
        # ``OpenAIProvider.__init__`` reads ``url``/``model`` from
        # ``_cfg`` with OpenAI-shaped fallbacks, so a minimal voyage
        # saga.toml that omits those keys must still get voyage's
        # values patched on here. See issue #149.
        from ._config_io import was_set_in_toml
        if not was_set_in_toml("embedding", "url"):
            self.url = "https://api.voyageai.com/v1/embeddings"
        if not was_set_in_toml("embedding", "model"):
            self.model = "voyage-4-lite"
        if not was_set_in_toml("embedding", "api_key_env"):
            self.api_key_env = "VOYAGE_API_KEY"
        # send_input_type is hardcoded True for voyage — non-negotiable
        # since voyage's models REQUIRE the input_type prefix. Only
        # log a warning when the operator EXPLICITLY set False in
        # saga.toml; the OpenAIProvider default is False and a fresh
        # voyage saga.toml without an explicit ``send_input_type`` key
        # shouldn't spuriously trip the warning.
        explicit_value = _cfg('embedding', 'send_input_type', None)
        if explicit_value is False:
            import logging
            logging.getLogger("saga.embeddings").warning(
                "[embedding] send_input_type=false was set for "
                "provider=voyage; voyage REQUIRES input_type for "
                "retrieval-quality embeddings — forcing True"
            )
        self.send_input_type = True


# ─── Provider Registry ────────────────────────────────────────────

_PROVIDERS = {
    "openai": OpenAIProvider,
    "voyage": VoyageProvider,
    "onnx": ONNXProvider,
    "local": LocalProvider,
}

#: Per-provider recommended ``[consolidation] similarity_threshold``
#: values for the THEMATIC pass (pass 2). Used when ``[consolidation]
#: similarity_threshold = "auto"`` is set in saga.toml — the value
#: resolves to the entry matching the configured embedding provider.
#:
#: **All providers now resolve to 0.80.** The earlier voyage=0.92 /
#: onnx=0.92 entries were picked by the "lowest threshold where the
#: 20-cluster cap stops firing" heuristic on Phase 1's kept DBs. But
#: re-calibration on muninn (n=1789) + mimir-saga (n=693) showed that
#: 0.80 forms the COHERENT thematic clusters (Phase 2 bench tracking,
#: heartbeat chains, Hailey-Tim-blog motifs) — the cap-saturation at
#: 0.80 isn't a noise problem, it's "more eligible clusters than the
#: pass-2 cap can absorb." The new pass-1 dedup at the 0.92 floor
#: (see ``DEFAULT_DEDUP_THRESHOLD`` in dedup.py) absorbs the template
#: near-duplicate noise BEFORE pass 2 runs, so the cap doesn't bind
#: against real thematic clusters.
#:
#: OpenAI 3-small calibration confirms 0.80 sits at the ~99.98th
#: percentile on conversational corpora (muninn-shape), exactly where
#: the historical 0.88 LongMemEval baseline scored.
#:
#: Falls back to 0.80 (saga's historical default) for providers without
#: an explicit entry.
_PROVIDER_AUTO_THRESHOLDS: dict[str, float] = {
    "openai": 0.80,
    "voyage": 0.80,
    "onnx": 0.80,
    "local": 0.80,
}


def resolve_auto_threshold(provider_name: str) -> float:
    """Resolve ``[consolidation] similarity_threshold = "auto"`` to a
    numeric value based on the configured embedding provider.

    Returns the entry from ``_PROVIDER_AUTO_THRESHOLDS`` matching
    ``provider_name``, or 0.80 (saga's historical default) for
    unrecognized providers.
    """
    return _PROVIDER_AUTO_THRESHOLDS.get(provider_name, 0.80)

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

    Auto-fallback: if the configured provider is API-keyed (``voyage``,
    ``openai``) and the required API key env var isn't set,
    transparently fall through to the ``onnx`` (fastembed) provider
    instead of raising on the first ``embed()`` call. Lets fresh
    ``mimir setup``-only installs work out of the box without a
    Voyage key, while paid-tier users keep the better embeddings as
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
        provider_name = _cfg('embedding', 'provider', 'voyage')

        # API-keyed providers — auto-fall-back to onnx when the
        # required env var isn't set. Voyage is the default
        # (``mimir setup --embedding voyage``); a fresh install
        # without VOYAGE_API_KEY should fall back to local fastembed
        # rather than raising on first embed.
        _API_KEYED_PROVIDERS = ("openai", "voyage")
        _DEFAULT_KEY_ENVS = {
            "openai": "OPENAI_API_KEY",
            "voyage": "VOYAGE_API_KEY",
        }
        # When falling back from an API-keyed provider to onnx, force
        # the onnx default model: the configured model is typically
        # the API model name and fastembed would reject it.
        _onnx_model_override: Optional[str] = None
        if provider_name in _API_KEYED_PROVIDERS:
            api_key_env = _cfg(
                'embedding', 'api_key_env',
                _DEFAULT_KEY_ENVS[provider_name],
            )
            if api_key_env and not os.environ.get(api_key_env):
                logging.getLogger("saga.embeddings").info(
                    "[embedding] provider=%s but %s is unset — falling back "
                    "to onnx (fastembed, BAAI/bge-small-en-v1.5). Set %s in "
                    "your environment to use %s.",
                    provider_name, api_key_env, api_key_env, provider_name,
                )
                provider_name = "onnx"
                _onnx_model_override = "BAAI/bge-small-en-v1.5"

        provider_cls = _PROVIDERS.get(provider_name)
        if provider_cls is None:
            raise ValueError(
                f"Unknown embedding provider: {provider_name}. "
                f"Available: {', '.join(_PROVIDERS.keys())}"
            )
        if _onnx_model_override is not None and provider_cls is ONNXProvider:
            _provider_instance = provider_cls(model_name=_onnx_model_override)
        else:
            _provider_instance = provider_cls()
    return _provider_instance


def embed_text(text: str) -> list[float]:
    """Embed text for storage (passage mode)."""
    max_chars = _cfg('embedding', 'max_input_chars', 2000)
    return get_provider().embed(text[:max_chars], input_type="passage")


def embed_query(text: str) -> list[float]:
    """Embed text for retrieval (query mode)."""
    max_chars = _cfg('embedding', 'max_input_chars', 2000)
    return get_provider().embed(text[:max_chars], input_type="query")


def batch_embed_texts(texts: list[str]) -> list[list[float]]:
    """Batch embed texts for storage (passage mode)."""
    max_chars = _cfg('embedding', 'max_input_chars', 2000)
    truncated = [t[:max_chars] for t in texts]
    return get_provider().batch_embed(truncated, input_type="passage")


@lru_cache(maxsize=64)
def cached_embed_query(text: str) -> tuple:
    """Cached query embedding. Returns tuple (hashable for LRU cache)."""
    return tuple(embed_query(text))
