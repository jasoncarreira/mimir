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
    """Route through claude-agent-sdk's ``query()`` — a one-shot Claude
    Code subprocess. Inherits OAuth from ``claude login`` (Max plan)
    so saga's internal LLM calls (consolidation synthesis, contextual
    rewrite, triple extraction, etc.) don't need a separate API key.

    Tradeoffs vs. ``anthropic``:
    - **Auth**: free under Max — no API credit needed.
    - **Latency**: ~500ms-2s subprocess spawn per call. For bench runs
      with many consolidation clusters this adds up; consider
      ``provider = "openai_compat"`` with gpt-5.4-nano for direct bench
      parity against ``saga_p30_canon_v4 = 0.774``.
    - **Quota**: counts against your 5h / 7d Max windows. Daily mimir
      use is fine; a 500-question bench can eat through quickly.
    - **Reproducibility**: Max plan throttles via wait, not 429. Bench
      numbers may drift more than direct-API runs.

    saga's ``call_llm_sync`` is a sync function; we bridge to the async
    ``query()`` via ``asyncio.run`` from a saga thread (every call site
    is wrapped in ``asyncio.to_thread`` at mimir's boundary, so we're
    not on the event loop here).

    The ``temperature`` and ``max_tokens`` knobs aren't honored by
    Claude Code — the CLI doesn't expose them. ``model`` from the llm
    dict overrides the CLI default; otherwise the user's
    ``CLAUDE_MODEL`` env or claude config wins.
    """
    try:
        # Lazy import: claude-agent-sdk lives in mimir's deps but saga
        # doesn't depend on mimir, and standalone saga environments may
        # not have it installed.
        from claude_agent_sdk import query, ClaudeAgentOptions
    except ImportError:
        log.warning("claude-agent-sdk not installed; falling back to openai_compat")
        return _call_openai_compat(
            llm, prompt=prompt, max_tokens=max_tokens,
            temperature=temperature, system=system,
        )

    import asyncio

    options_kwargs: dict[str, Any] = {}
    model = llm.get("model")
    if model:
        options_kwargs["model"] = model
    if system:
        options_kwargs["system_prompt"] = system

    async def _run() -> str:
        pieces: list[str] = []
        try:
            async for msg in query(
                prompt=prompt,
                options=ClaudeAgentOptions(**options_kwargs),
            ):
                # AssistantMessage is the one with content blocks. Other
                # types (ResultMessage, SystemMessage) we ignore — saga
                # prompts don't request tools, only text.
                content = getattr(msg, "content", None)
                if not content:
                    continue
                for block in content:
                    text = getattr(block, "text", None)
                    if text:
                        pieces.append(text)
        except Exception as exc:  # noqa: BLE001
            log.warning("claude-agent-sdk query failed: %s", exc)
            return ""
        return "".join(pieces).strip()

    try:
        return asyncio.run(_run())
    except RuntimeError as exc:
        # asyncio.run raises if we're already inside an event loop. saga's
        # hot paths run via asyncio.to_thread (i.e., on a worker thread
        # without a loop), so this is rare — only happens if a caller is
        # using saga directly from inside an async context.
        log.warning(
            "claude_code provider can't bridge an existing event loop "
            "(%s); falling back to openai_compat",
            exc,
        )
        return _call_openai_compat(
            llm, prompt=prompt, max_tokens=max_tokens,
            temperature=temperature, system=system,
        )


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
        # raise_for_status keeps compatibility with the original call-site
        # pattern (and saga's tests' fake response objects). Non-200 →
        # exception → logged + empty string returned.
        if r.status_code >= 400:
            # Surface the response body so model/parameter mismatches
            # are visible. raise_for_status alone shows only "400 Client
            # Error" without the JSON body explaining what's wrong
            # (e.g., "Unsupported parameter: 'max_tokens'...").
            body_preview = (getattr(r, "text", "") or "")[:300]
            log.warning(
                "openai_compat call failed: %s %s — body=%s",
                r.status_code, r.reason or "", body_preview,
            )
            return ""
        msg = r.json()["choices"][0]["message"]
        # Some models (e.g., step-3.5-flash) put answer in ``reasoning`` when
        # ``content`` is null.
        return (msg.get("content") or msg.get("reasoning") or "").strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("openai_compat call failed: %s", exc)
        return ""
