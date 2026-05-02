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


def _call_openai_compat(
    llm: dict[str, Any], *,
    prompt: str, max_tokens: int, temperature: float,
    system: str | None,
) -> str:
    """Legacy path — the original requests.post implementation, lifted
    here unchanged so call sites can drop their inline blocks.

    Some servers (gpt-5.x family) require ``max_completion_tokens``
    instead of ``max_tokens``; we send both for compatibility — extra
    keys are ignored by servers that don't recognize them. (The
    original consolidation.py fix landed in commit a426959 before this
    refactor; this helper preserves the semantics.)
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
        "max_tokens": max_tokens,
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
        r.raise_for_status()
        msg = r.json()["choices"][0]["message"]
        # Some models (e.g., step-3.5-flash) put answer in ``reasoning`` when
        # ``content`` is null.
        return (msg.get("content") or msg.get("reasoning") or "").strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("openai_compat call failed: %s", exc)
        return ""
