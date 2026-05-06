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

Per-subsystem ``[<section>] provider = "..."`` overrides
``[llm].provider``. See ``config.resolve_llm_config``.

The returned text follows the OpenAI-compatible "single best
candidate" shape: a single string. Anthropic's response can have
multiple content blocks (text + tool_use); we collapse to text only
(saga's calls don't request tools).
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def call_llm_sync(
    llm: dict[str, Any],
    *,
    prompt: str,
    max_tokens: int = 1500,
    temperature: float = 0.3,
    system: str | None = None,
) -> str:
    """Synchronous LLM call. saga's hot paths are sync (def, not async
    def); they call this directly. The async wrapping for mimir's
    event-loop responsiveness happens at mimir's boundary
    (``_InProcessSaga.<method>`` wraps with ``asyncio.to_thread``).

    Returns the assistant's reply text. Empty string on transport
    failure — callers handle gracefully (every existing site already
    does, since the old ``requests.post`` path raised exceptions that
    were caught).
    """
    provider = (llm.get("provider") or "openai_compat").lower()
    if provider == "anthropic":
        return _call_anthropic(
            llm, prompt=prompt, max_tokens=max_tokens,
            temperature=temperature, system=system,
        )
    if provider == "claude_code":
        return _call_claude_code(
            llm, prompt=prompt, max_tokens=max_tokens,
            temperature=temperature, system=system,
        )
    return _call_openai_compat(
        llm, prompt=prompt, max_tokens=max_tokens,
        temperature=temperature, system=system,
    )


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


def _call_claude_code(
    llm: dict[str, Any], *,
    prompt: str, max_tokens: int, temperature: float,
    system: str | None,
) -> str:
    """Route through claude-agent-sdk against a long-lived
    ``ClaudeSDKClient`` so the Claude Code subprocess stays warm
    across saga's many small LLM calls. Inherits OAuth from
    ``claude login`` (Max plan).

    Why the persistent client matters:
      Measured one-shot ``query()`` startup: ~5-9s/call (most of it
      subprocess spawn).
      Measured warm ``ClaudeSDKClient.query()``: ~1.5-2s/call.
      For a 500-question integration bench with ~30 consolidation
      clusters per question, that's the difference between ~30 hours
      and ~8 hours of consolidation wall-clock.

    Caveat: ``ClaudeSDKClient`` accumulates conversation history
    across ``query()`` calls — call 2 remembers what call 1 said.
    saga's prompts are independent (one consolidation cluster has
    nothing to do with the next), so the carry-over is pure waste.
    We recycle the client every ``RECYCLE_AFTER`` calls
    (``disconnect`` + new ``connect``) to bound the context bloat.
    Recycle cost is one cold-start (~1s) every K calls; net win is
    still ~3-4x.

    Other tradeoffs vs. ``anthropic`` / ``openai_compat``:
    - **Auth**: free under Max — no API credit needed.
    - **Quota**: counts against your 5h / 7d Max windows. Daily
      mimir use is fine; a 500-question bench can eat through.
    - **Reproducibility**: Max plan throttles via wait, not 429.
      Bench numbers may drift more than direct-API runs.

    ``temperature`` and ``max_tokens`` aren't honored — the Claude
    Code CLI doesn't expose them. ``model`` from the llm dict picks
    the model; otherwise ``CLAUDE_MODEL`` env or CLI default wins.
    """
    try:
        # Lazy import: claude-agent-sdk lives in mimir's deps but saga
        # doesn't depend on mimir, and standalone saga environments may
        # not have it installed.
        from claude_agent_sdk import (  # noqa: F401 — used by _PersistentClaudeCode
            ClaudeAgentOptions,
            ClaudeSDKClient,
        )
    except ImportError:
        log.warning("claude-agent-sdk not installed; falling back to openai_compat")
        return _call_openai_compat(
            llm, prompt=prompt, max_tokens=max_tokens,
            temperature=temperature, system=system,
        )

    pool = _get_persistent_pool()
    runner = pool.acquire()
    try:
        return runner.call(
            prompt=prompt,
            model=llm.get("model"),
            system=system,
        )
    finally:
        pool.release(runner)


# ─── Persistent ClaudeSDKClient ──────────────────────────────────


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
import threading as _threading

_DEFAULT_POOL_SIZE = 4
"""Default ceiling on concurrent ``_PersistentClaudeCode`` instances.

4 covers mimir's expected steady-state (~2 chat channels active at
once + 1 saga consolidation cron) with headroom. Override via
``SAGA_PERSISTENT_CLAUDE_POOL_SIZE``.

Why bounded vs. the previous ``threading.local`` cache: under
``asyncio.to_thread`` worker churn (mimir's call path), each fresh
worker thread that hit ``_persistent_runner()`` allocated a new
``_PersistentClaudeCode`` — daemon thread + event loop + Claude Code
subprocess. The default ``ThreadPoolExecutor`` grows up to ``min(32,
os.cpu_count() + 4)`` workers, so on an 8-core box that meant up to
12 daemon threads each holding ~50MB of subprocess RSS, never
shrinking. The pool fixes the leak by capping live instances and
recycling them across worker threads via FIFO checkout."""


class _PersistentClaudePool:
    """Bounded thread-safe pool of ``_PersistentClaudeCode`` instances.

    Replaces the per-thread ``threading.local`` cache. Cross-thread
    parallelism is preserved up to ``max_size`` concurrent callers —
    important for cross-channel mimir traffic where Discord and
    Slack turns can both hit saga LLM at the same time. Once
    ``max_size`` is in flight, additional callers block on
    ``acquire`` until one is returned.

    Construction is cheap (~5ms — daemon thread + event loop spin-up;
    the Claude Code SDK client is lazy-connected on first
    ``runner.call(...)``), so we don't pre-warm the pool. Instances
    are created on demand up to the cap; once created they live
    until process exit.

    Not asyncio-aware — saga's contract is sync from any Python
    thread (``call_llm_sync``). Synchronization is via a
    ``threading.Condition``; callers may be on any thread. (The
    inner ``_PersistentClaudeCode`` does its own sync→async bridge
    via its daemon-thread event loop.)"""

    def __init__(self, max_size: int) -> None:
        if max_size < 1:
            raise ValueError(f"max_size must be >= 1, got {max_size}")
        self._max_size = max_size
        self._idle: list[_PersistentClaudeCode] = []
        self._size = 0  # idle + in-flight, never decremented
        self._cond = _threading.Condition()

    @property
    def max_size(self) -> int:
        return self._max_size

    @property
    def size(self) -> int:
        with self._cond:
            return self._size

    def acquire(self) -> "_PersistentClaudeCode":
        """Block until an instance is available; return it. Caller
        MUST call ``release`` when done."""
        with self._cond:
            while True:
                if self._idle:
                    return self._idle.pop()
                if self._size < self._max_size:
                    # Reserve the slot before releasing the lock so
                    # concurrent acquirers don't double-grow past
                    # ``max_size``. Construction happens outside the
                    # lock — daemon-thread spin-up + event-loop
                    # ready-wait is bounded but non-trivial; holding
                    # the lock would serialize cold-start.
                    self._size += 1
                    break
                self._cond.wait()
        try:
            return _PersistentClaudeCode()
        except BaseException:
            # Construction failed — back out the reservation and
            # wake any waiters. The slot is free again.
            with self._cond:
                self._size -= 1
                self._cond.notify()
            raise

    def release(self, runner: "_PersistentClaudeCode") -> None:
        """Return an instance to the idle list and wake one waiter."""
        with self._cond:
            self._idle.append(runner)
            self._cond.notify()


_persistent_pool: "_PersistentClaudePool | None" = None
_pool_init_lock = _threading.Lock()


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


def _get_persistent_pool() -> "_PersistentClaudePool":
    """Lazy module-level singleton. Double-checked under a lock so
    concurrent first-callers don't construct two pools."""
    global _persistent_pool
    pool = _persistent_pool
    if pool is not None:
        return pool
    with _pool_init_lock:
        if _persistent_pool is None:
            _persistent_pool = _PersistentClaudePool(
                max_size=_resolve_pool_size()
            )
        return _persistent_pool


class _PersistentClaudeCode:
    """A daemon thread running an asyncio event loop with one warm
    ``ClaudeSDKClient``. saga's sync ``call_llm_sync`` submits prompts
    via ``run_coroutine_threadsafe`` and blocks on the result.

    Recycles the client every ``_RECYCLE_AFTER_CALLS`` calls (or when
    ``model`` changes between calls) to bound conversation bloat.

    Lifetime is managed by ``_PersistentClaudePool``: the pool caps
    live instances at ``max_size`` (default 4, override via
    ``SAGA_PERSISTENT_CLAUDE_POOL_SIZE``) and lets callers borrow via
    ``acquire``/``release``. Each instance owns its own inner
    event-loop thread, its own Claude Code subprocess, and its own
    submit lock. Concurrent ``call`` invocations on the *same*
    instance serialize on ``_submit_lock``; the pool ensures distinct
    callers get distinct instances up to ``max_size``, preserving
    cross-channel parallelism without leaking instances per worker
    thread the way the previous ``threading.local`` cache did."""

    def __init__(self) -> None:
        import asyncio
        import os
        import threading
        from concurrent.futures import Future

        self._asyncio = asyncio
        self._Future = Future
        recycle_env = os.environ.get("SAGA_PERSISTENT_CLAUDE_RECYCLE", "")
        try:
            self._recycle_after = max(1, int(recycle_env)) if recycle_env else _RECYCLE_AFTER_CALLS
        except ValueError:
            self._recycle_after = _RECYCLE_AFTER_CALLS

        # Daemon thread runs the event loop. Daemon=True so the bench
        # runner's process exits without us hanging on close cleanup
        # — the OS reaps the Claude Code subprocess on parent exit.
        self._loop_ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="saga-claude-code")
        self._thread.start()
        self._loop_ready.wait(timeout=5)
        if self._loop is None:
            raise RuntimeError("persistent claude-code thread failed to start")

        self._client = None  # type: ignore[assignment]
        self._client_model: str | None = None
        self._call_count = 0
        # Submission lock: ClaudeSDKClient is single-threaded; saga
        # call_llm_sync usages are sequential per-thread but we lock
        # to make this safe under unexpected concurrency too.
        self._submit_lock = threading.Lock()

    def _run_loop(self) -> None:
        loop = self._asyncio.new_event_loop()
        self._asyncio.set_event_loop(loop)
        self._loop = loop
        self._loop_ready.set()
        try:
            loop.run_forever()
        finally:
            loop.close()

    def call(self, *, prompt: str, model: str | None, system: str | None) -> str:
        """Submit a prompt to the warm client. Blocks the caller until
        the assistant's reply is fully received, then returns the
        flattened text."""
        with self._submit_lock:
            future = self._asyncio.run_coroutine_threadsafe(
                self._do_call(prompt=prompt, model=model, system=system),
                self._loop,  # type: ignore[arg-type]
            )
            try:
                return future.result(timeout=600)
            except Exception as exc:  # noqa: BLE001
                log.warning("persistent claude-code call failed: %s", exc)
                return ""

    async def _do_call(
        self, *, prompt: str, model: str | None, system: str | None
    ) -> str:
        from claude_agent_sdk import (
            AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient,
        )

        # Recycle conditions: model changed (different options needed)
        # OR call count hit the recycle threshold.
        if (
            self._client is None
            or self._client_model != model
            or self._call_count >= self._recycle_after
        ):
            await self._reset_client(model=model, system=system)

        assert self._client is not None
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

    async def _reset_client(self, *, model: str | None, system: str | None) -> None:
        """Disconnect any current client + spin up a fresh one. Cheap
        relative to one-shot ``query()`` startup because most of the
        connect cost amortizes over the next K calls."""
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception as exc:  # noqa: BLE001
                log.warning("persistent claude-code disconnect failed: %s", exc)
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
