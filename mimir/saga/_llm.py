"""Single LLM transport for saga's chat-completion call sites.

Saga makes ~9 LLM calls across the codebase: contextual rewrite, HyDE,
combined rewrite+HyDE, consolidation, triple extraction (single +
batch), atom annotation, retrieval_v2 path, subatom extraction. All
of them used to do raw ``requests.post`` to OpenAI-compatible
chat-completions endpoints. v0.5 §7 routes them through ``call_llm``,
which delegates to either:

- **anthropic** (default for production) — ``anthropic.Anthropic``
  with the Messages API. Plan-window utilization rolls into mimir's
  ``rate_limits.py`` surface so saga's consolidation cron is visible
  in the same window the agent reports against.
- **openai_compat** (default for the LongMemEval bench harness) —
  the legacy ``requests.post`` path, kept so bench numbers stay
  comparable to the post-fix ``saga_p30_canon_v4`` baseline (0.774,
  characterized against gpt-5.4-nano).
- **codex_plus** — ``langchain_codex_plus.ChatCodexPlus`` over the
  ChatGPT/Codex subscription (OAuth from ``$CODEX_HOME/auth.json``,
  default ``~/.codex``). Like claude_code it shares the operator's
  plan-window quota and needs no API credit; native async (ainvoke).

Per-subsystem ``[<section>] provider = "..."`` overrides
``[llm].provider``. See ``config.resolve_llm_config``.

The returned text follows the OpenAI-compatible "single best
candidate" shape: a single string. Anthropic's response can have
multiple content blocks (text + tool_use); we collapse to text only
(saga's calls don't request tools).

Single async entry point:

- ``async def call_llm(...)`` — async-native (chainlink #20). The
  claude_code and codex_plus paths are native async on the caller's
  event loop (claude_code via ``_AsyncClaudePool``; codex_plus via
  ``ChatCodexPlus.ainvoke``). ``anthropic`` and ``openai_compat`` are
  sync HTTP libraries; the wrapper offloads them via
  ``asyncio.to_thread`` so the loop stays responsive.

The legacy ``call_llm_sync`` + ``_PersistentClaudePool`` daemon-thread
bridge was deleted in Phase 3 — saga's internals are now async-native.

Retry behavior (chainlink #841):
- Transient provider errors (429, 5xx, overloaded, connection/timeout)
  are retried with exponential backoff + jitter.
- Non-transient errors (400, auth/401/403, context-length, content-policy)
  fail fast without retry.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from mimir._llm_retry import _retry_async, _retry_sync

log = logging.getLogger(__name__)


def _call_anthropic(
    llm: dict[str, Any], *,
    prompt: str, max_tokens: int, temperature: float,
    system: str | None,
) -> str:
    api_key = llm.get("api_key") or ""
    if not api_key:
        log.warning("anthropic provider selected but no api_key resolved; returning empty")
        return ""
    try:
        # Lazy import: keep the openai_compat path runnable in environments
        # where anthropic isn't installed (e.g., older bench infra).
        from anthropic import Anthropic
    except ImportError:
        log.warning("anthropic SDK not installed; falling back to openai_compat")
        return _call_openai_compat(
            llm, prompt=prompt, max_tokens=max_tokens,
            temperature=temperature, system=system,
        )

    client = Anthropic(api_key=api_key, timeout=llm.get("timeout", 30))
    try:
        msg = client.messages.create(
            model=llm.get("model") or "claude-haiku-4-5",
            max_tokens=max_tokens,
            temperature=temperature,
            system=system or "",
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:  # noqa: BLE001 — saga's existing sites swallow.
        log.warning("anthropic call failed: %s", exc)
        return ""

    # Anthropic returns a list of content blocks (text + optional tool_use,
    # thinking). We flatten to text only; saga's prompts don't request
    # tool use.
    pieces: list[str] = []
    for block in msg.content or []:
        text = getattr(block, "text", None)
        if text:
            pieces.append(text)
    return "".join(pieces).strip()


# ─── ClaudeSDKClient pool tunables ───────────────────────────────


_RECYCLE_AFTER_CALLS = 10
"""Recycle the ClaudeSDKClient every N saga LLM calls.

ClaudeSDKClient accumulates conversation history across query() calls.
Saga's prompts are independent, so the carry-over is wasted tokens (and
on Max OAuth, wasted plan-window quota). 10 strikes a balance: ~10x
amortization on the cold-start cost vs. ~10 × prompt_size tokens of
peak context at the high end of each cycle. Adjust via the
``SAGA_PERSISTENT_CLAUDE_RECYCLE`` env var if a particular workload
benefits from a different K."""


import os as _os

_DEFAULT_POOL_SIZE = 4
"""Default ceiling on concurrent ``_AsyncClaudeRunner`` instances.

4 covers mimir's expected steady-state (~2 chat channels active at
once + 1 saga consolidation cron) with headroom. Override via
``SAGA_PERSISTENT_CLAUDE_POOL_SIZE`` (env name kept for backward
compatibility with operator-set values from the sync-pool era)."""


def _resolve_pool_size() -> int:
    raw = _os.environ.get("SAGA_PERSISTENT_CLAUDE_POOL_SIZE", "")
    if not raw:
        return _DEFAULT_POOL_SIZE
    try:
        n = int(raw)
    except ValueError:
        log.warning(
            "SAGA_PERSISTENT_CLAUDE_POOL_SIZE=%r is not an int; "
            "falling back to default %d",
            raw, _DEFAULT_POOL_SIZE,
        )
        return _DEFAULT_POOL_SIZE
    if n < 1:
        log.warning(
            "SAGA_PERSISTENT_CLAUDE_POOL_SIZE=%d < 1 is not valid; "
            "falling back to default %d",
            n, _DEFAULT_POOL_SIZE,
        )
        return _DEFAULT_POOL_SIZE
    return n


def _call_openai_compat(
    llm: dict[str, Any], *,
    prompt: str, max_tokens: int, temperature: float,
    system: str | None,
) -> str:
    """Legacy path — the original requests.post implementation, lifted
    here unchanged so call sites can drop their inline blocks.

    Sends ``max_completion_tokens`` (the newer OpenAI key). gpt-5.x
    rejects ``max_tokens`` outright (400: ``Unsupported parameter:
    'max_tokens' is not supported with this model. Use
    'max_completion_tokens' instead.``); older models accept the new
    name too. We previously sent both for back-compat — that broke
    when gpt-5.4-nano started rejecting unknown extras (rather than
    silently ignoring them).
    """
    import requests

    api_key = llm.get("api_key") or ""
    url = llm.get("url") or ""
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    body: dict[str, Any] = {
        "model": llm.get("model") or "",
        "messages": messages,
        "temperature": temperature,
        "max_completion_tokens": max_tokens,
    }
    try:
        r = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=llm.get("timeout", 30),
        )
        # Non-200 → HTTPError → logged + empty string returned. Use
        # raise_for_status (not a status_code check) for compatibility
        # with saga's test fakes that only stub raise_for_status + json.
        try:
            r.raise_for_status()
        except Exception as http_exc:  # noqa: BLE001
            # Surface the response body so model/parameter mismatches are
            # visible — raise_for_status alone shows only "400 Client
            # Error" without the JSON body that explains what's wrong
            # (e.g., "Unsupported parameter: 'max_tokens'...").
            body_preview = (getattr(r, "text", "") or "")[:300]
            log.warning(
                "openai_compat call failed: %s — body=%s",
                http_exc, body_preview,
            )
            return ""
        msg = r.json()["choices"][0]["message"]
        # Some models (e.g., step-3.5-flash) put answer in ``reasoning`` when
        # ``content`` is null.
        return (msg.get("content") or msg.get("reasoning") or "").strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("openai_compat call failed: %s", exc)
        return ""


# ─── Async-native call_llm (chainlink #20) ──────────


async def call_llm(
    llm: dict[str, Any],
    *,
    prompt: str,
    max_tokens: int = 1500,
    temperature: float = 0.3,
    system: str | None = None,
) -> str:
    """Async-native LLM call. Saga's sole entry point — callers on an
    event loop ``await`` this directly.

    Provider dispatch:
    - ``claude_code`` runs on the caller's event loop via
      ``_AsyncClaudePool`` — bounded pool of ``ClaudeSDKClient``
      instances connected on the running loop, recycle-after-N-calls.
    - ``anthropic`` and ``openai_compat`` are sync HTTP libraries
      (``anthropic.Anthropic.messages.create`` and ``requests.post``);
      we offload them to a thread via ``asyncio.to_thread`` so the
      caller's loop stays responsive.

    Returns the assistant's reply text. Empty string on transport
    failure — every existing caller already handles empty gracefully.

    Retry behavior: Transient provider errors are retried with exponential
    backoff + jitter (see ``mimir._llm_retry``)."""
    raw_provider = llm.get("provider") or "openai_compat"
    provider = raw_provider.lower()
    if provider == "claude_code":
        return await _retry_async(
            _call_claude_code_async,
            llm, prompt=prompt, max_tokens=max_tokens,
            temperature=temperature, system=system,
            provider=provider,
        )
    if provider == "codex_plus":
        return await _retry_async(
            _call_codex_plus_async,
            llm, prompt=prompt, max_tokens=max_tokens,
            temperature=temperature, system=system,
            provider=provider,
        )
    if provider == "anthropic":
        return await asyncio.to_thread(
            _retry_sync,
            _call_anthropic, llm,
            prompt=prompt, max_tokens=max_tokens,
            temperature=temperature, system=system,
            provider=provider,
        )
    # ``openai_compat`` is the documented fallback name. Anything else
    # is a typo or stale enum value — warn so the operator can spot it
    # in the log. Pre-fix, ``provider="anthropic-sdk"`` (missing
    # underscore) silently called api.openai.com under a different
    # cost + token-accounting regime; that footgun stays exactly as
    # silent as the operator's eyes are sharp without this warning.
    if provider != "openai_compat":
        log.warning(
            "call_llm: unknown provider %r — falling back to openai_compat. "
            "Recognized: 'claude_code', 'codex_plus', 'anthropic', 'openai_compat'.",
            raw_provider,
        )
    return await asyncio.to_thread(
        _retry_sync,
        _call_openai_compat, llm,
        prompt=prompt, max_tokens=max_tokens,
        temperature=temperature, system=system,
        provider="openai_compat",
    )


def _flatten_text(content: Any) -> str:
    """Collapse a chat model's ``.content`` to plain text.

    ChatCodexPlus (responses API) may return either a string or a list
    of content blocks (dicts with a ``text`` key, or objects with a
    ``.text`` attr). saga's prompts never request tools, so text is all
    we want."""
    if isinstance(content, str):
        return content.strip()
    pieces: list[str] = []
    for block in content or []:
        if isinstance(block, str):
            pieces.append(block)
        elif isinstance(block, dict):
            pieces.append(block.get("text") or "")
        else:
            pieces.append(getattr(block, "text", "") or "")
    return "".join(pieces).strip()


async def _call_codex_plus_async(
    llm: dict[str, Any], *,
    prompt: str, max_tokens: int, temperature: float,
    system: str | None,
) -> str:
    """codex_plus path — saga's LLM calls ride the ChatGPT/Codex
    subscription via ``langchain_codex_plus.ChatCodexPlus`` (OAuth from
    ``$CODEX_HOME/auth.json``, default ``~/.codex``). Like the
    claude_code path it shares the operator's plan-window quota and
    needs no API credit; unlike it there's no subprocess — ChatCodexPlus
    is a native async chat model, so we ``await`` it directly on the
    caller's loop.

    ``max_tokens``/``temperature`` are accepted but unused: the Codex
    responses API (as surfaced by ChatCodexPlus) doesn't expose them.
    ``reasoning_effort="none"`` keeps inference cheap and matches mimir's
    main-agent codex-plus construction (agent.py).

    A fresh ChatCodexPlus is built per call so its async HTTP client
    binds to the *calling* loop — saga runs on different loops across
    consolidation crons vs. chat turns, and a cached client bound to a
    closed loop would raise. Construction is cheap (pydantic init; the
    auth bundle is a small local file the lib reads + refreshes on 401,
    persisting atomically). Returns "" on any failure — every saga call
    site already handles empty.

    NOTE: saga's codex_plus usage consumes the same subscription quota
    as the main agent server-side, but isn't yet wired into mimir's
    local ``rate_limits`` surface (no ``rate_limit_callback`` here) —
    tracked as a follow-up."""
    try:
        from langchain_codex_plus import ChatCodexPlus
        from langchain_core.messages import HumanMessage, SystemMessage
    except ImportError:
        # codex_plus selected but the extra isn't installed. Return ""
        # (saga degrades gracefully) rather than silently re-routing to a
        # different, possibly-paid provider — the misconfig is logged.
        log.warning(
            "codex_plus provider selected but langchain-codex-plus is not "
            "installed; returning empty. Install the 'codex-plus' extra."
        )
        return ""

    messages: list[Any] = []
    if system:
        messages.append(SystemMessage(content=system))
    messages.append(HumanMessage(content=prompt))
    try:
        chat = ChatCodexPlus(
            model=llm.get("model") or "gpt-5.4",
            reasoning_effort="none",
            timeout_seconds=float(llm.get("timeout", 120)),
        )
        result = await chat.ainvoke(messages)
    except Exception as exc:  # noqa: BLE001 — saga's call sites swallow.
        log.warning("codex_plus call failed: %s", exc)
        return ""
    return _flatten_text(result.content)


async def _call_claude_code_async(
    llm: dict[str, Any], *,
    prompt: str, max_tokens: int, temperature: float,
    system: str | None,
) -> str:
    """Async claude_code path. Acquires an ``_AsyncClaudeRunner`` from
    the per-loop pool, calls it, releases on completion. ``max_tokens``
    and ``temperature`` are accepted but unused — the Claude Code CLI
    doesn't expose them (matches the sync claude_code path)."""
    try:
        from claude_agent_sdk import (  # noqa: F401 — used by _AsyncClaudeRunner
            ClaudeAgentOptions,
            ClaudeSDKClient,
        )
    except ImportError:
        log.warning("claude-agent-sdk not installed; falling back to openai_compat")
        import asyncio
        return await asyncio.to_thread(
            _call_openai_compat, llm,
            prompt=prompt, max_tokens=max_tokens,
            temperature=temperature, system=system,
        )

    pool = _get_async_claude_pool()
    runner = await pool.acquire()
    try:
        return await runner.call(
            prompt=prompt,
            model=llm.get("model"),
            system=system,
        )
    finally:
        await pool.release(runner)


class _AsyncClaudeRunner:
    """Owns one ``ClaudeSDKClient`` connected on the running event
    loop. ``call`` runs on the caller's loop; no daemon thread, no
    cross-thread bridge.

    Recycles the client every ``recycle_after`` calls (or when
    ``model`` changes between calls) to bound conversation bloat.

    Lifetime is managed by ``_AsyncClaudePool``: the pool caps live
    instances at its ``max_size`` and lets callers borrow via
    ``acquire``/``release``."""

    def __init__(self, recycle_after: int) -> None:
        self._recycle_after = recycle_after
        self._client = None  # type: ignore[assignment]
        self._client_model: str | None = None
        self._call_count = 0

    @property
    def call_count(self) -> int:
        return self._call_count

    async def call(self, *, prompt: str, model: str | None, system: str | None) -> str:
        """Submit a prompt to the warm client and return the flattened
        reply text. Empty string on transport failure (matches
        sync-path semantics so saga's existing call sites swallow
        gracefully)."""
        try:
            from claude_agent_sdk import AssistantMessage
        except ImportError:
            log.warning("claude-agent-sdk not installed; returning empty")
            return ""

        if (
            self._client is None
            or self._client_model != model
            or self._call_count >= self._recycle_after
        ):
            await self._reset_client(model=model, system=system)

        assert self._client is not None
        try:
            await self._client.query(prompt)
            pieces: list[str] = []
            async for msg in self._client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content or []:
                        text = getattr(block, "text", None)
                        if text:
                            pieces.append(text)
            self._call_count += 1
            return "".join(pieces).strip()
        except Exception as exc:  # noqa: BLE001
            log.warning("async claude-code call failed: %s", exc)
            return ""

    async def _reset_client(self, *, model: str | None, system: str | None) -> None:
        """Disconnect any current client + spin up a fresh one connected
        on the running loop."""
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception as exc:  # noqa: BLE001
                log.warning("async claude-code disconnect failed: %s", exc)
            self._client = None

        options_kwargs: dict[str, Any] = {}
        if model:
            options_kwargs["model"] = model
        if system:
            options_kwargs["system_prompt"] = system

        client = ClaudeSDKClient(options=ClaudeAgentOptions(**options_kwargs))
        await client.connect()
        self._client = client
        self._client_model = model
        self._call_count = 0

    async def aclose(self) -> None:
        """Disconnect the underlying client. Called when the pool is
        torn down (e.g., between tests with isolated event loops)."""
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception as exc:  # noqa: BLE001
                log.warning("async claude-code aclose disconnect failed: %s", exc)
            self._client = None


from .async_pool import BoundedAsyncPool


class _AsyncClaudePool(BoundedAsyncPool["_AsyncClaudeRunner"]):
    """Bounded asyncio-native pool of ``_AsyncClaudeRunner`` instances.

    Tied to a single event loop — the ``asyncio.Condition`` (inherited
    via ``BoundedAsyncPool``) binds to the running loop on first
    ``acquire``/``release``. Different loops (e.g., test isolation)
    need different pool instances; see ``_get_async_claude_pool`` for
    the per-loop registry that enforces this.

    Cross-task parallelism is preserved up to ``max_size`` concurrent
    callers — important for cross-channel mimir traffic where Discord
    and Slack turns can both hit saga LLM at the same time. Once
    ``max_size`` is in flight, additional callers ``await`` until one
    is returned.

    Recycle policy is per-runner (the runner tracks its own call count
    and reconnects every N calls). The pool itself doesn't churn
    runners — once created they live until the pool is closed."""

    def __init__(self, max_size: int, recycle_after: int) -> None:
        super().__init__(max_size)
        self._recycle_after = recycle_after
        self._size = 0  # idle + in-flight, never decremented

    @property
    def size(self) -> int:
        return self._size

    async def acquire(self) -> "_AsyncClaudeRunner":
        """Block (await) until an instance is available; return it.
        Caller MUST call ``release`` when done."""
        cond = self._condition()
        async with cond:
            while True:
                if self._idle:
                    return self._idle.pop()
                if self._size < self._max_size:
                    # Reserve the slot before constructing so concurrent
                    # acquirers don't double-grow past max_size.
                    self._size += 1
                    try:
                        return _AsyncClaudeRunner(recycle_after=self._recycle_after)
                    except BaseException:
                        # Construction failed — back out and re-raise.
                        self._size -= 1
                        cond.notify()
                        raise
                await cond.wait()

    async def release(self, runner: "_AsyncClaudeRunner") -> None:
        """Return an instance to the idle list and wake one waiter."""
        cond = self._condition()
        async with cond:
            self._idle.append(runner)
            cond.notify()

    async def aclose(self) -> None:
        """Disconnect all idle runners and reset state. Used by tests
        to tear down between event loops, and would be called at saga
        shutdown if saga grew an explicit shutdown hook (it doesn't
        today — daemon-thread reaping by the OS is the production
        teardown path, and that goes away in Phase 3)."""
        cond = self._condition()
        async with cond:
            idle = self._idle
            self._idle = []
            self._size = 0
        for runner in idle:
            await runner.aclose()


# Per-loop async-pool registry. Different event loops (production,
# tests with isolated loops) need their own pool. ``WeakKeyDictionary``
# keys on the loop object; when the loop is garbage-collected, its
# entry vanishes automatically, so a long-lived process with churning
# loops (long pytest-asyncio runs) doesn't accumulate stale pools.
# For deterministic cleanup, callers can use ``_reset_async_pools()``
# to drop the registry without waiting for GC.

import weakref as _weakref

_async_pools: "_weakref.WeakKeyDictionary[Any, _AsyncClaudePool]" = _weakref.WeakKeyDictionary()


def _resolve_recycle_after() -> int:
    raw = _os.environ.get("SAGA_PERSISTENT_CLAUDE_RECYCLE", "")
    if not raw:
        return _RECYCLE_AFTER_CALLS
    try:
        n = int(raw)
    except ValueError:
        log.warning(
            "SAGA_PERSISTENT_CLAUDE_RECYCLE=%r is not an int; falling back to %d",
            raw, _RECYCLE_AFTER_CALLS,
        )
        return _RECYCLE_AFTER_CALLS
    if n < 1:
        log.warning(
            "SAGA_PERSISTENT_CLAUDE_RECYCLE=%d < 1 is not valid; falling back to %d",
            n, _RECYCLE_AFTER_CALLS,
        )
        return _RECYCLE_AFTER_CALLS
    return n


def _get_async_claude_pool() -> _AsyncClaudePool:
    """Lazy per-running-loop singleton. Caller must already be on an
    event loop."""
    import asyncio
    loop = asyncio.get_running_loop()
    pool = _async_pools.get(loop)
    if pool is None:
        pool = _AsyncClaudePool(
            max_size=_resolve_pool_size(),
            recycle_after=_resolve_recycle_after(),
        )
        _async_pools[loop] = pool
    return pool


def _reset_async_pools() -> None:
    """Drop the per-loop pool registry without disconnecting clients.
    Used by tests to rebuild state between isolated event loops; in
    production this is never called.

    The previous loop's pool entries are left to be garbage-collected
    along with their loop. If a test wants a clean disconnect, it
    should ``await pool.aclose()`` first."""
    _async_pools.clear()
