"""Wiring for the ``gepa`` prompt-optimization skill (chainlink #332 / #405).

``gepa`` — the standalone reflective-prompt optimizer
(https://github.com/gepa-ai/gepa) — is **not** a mimir core dependency.
It ships as the opt-in ``gepa`` extra (``pip install 'mimir-agent[gepa]'``
or ``uv sync --extra gepa``): the optimizer is only needed while a GEPA
pilot runs, so importing mimir never pulls it in. Nothing in this module
imports ``gepa``.

What this module provides is the glue the chainlink #332 plan called the
"GEPAAdapter": routing gepa's ``reflection_lm`` (and a pilot's task
model) through mimir's already-configured ChatModel — codex-plus,
minimax, anthropic, etc. — instead of needing a separate LiteLLM /
OpenAI key.

gepa 0.1.1 exposes no ``langchain`` extra (the original plan assumed
``gepa[langchain]``); the supported hook for a custom provider is a
plain ``reflection_lm(prompt: str) -> str`` callable, which is what
:func:`chat_model_as_reflection_lm` returns::

    import gepa
    from mimir.gepa_support import reflection_lm_from_config

    result = gepa.optimize(
        seed_candidate={"instructions": BASELINE},
        trainset=examples,
        adapter=my_pilot_adapter,          # per-pilot evaluator; chainlink #404
        reflection_lm=reflection_lm_from_config(),
        max_metric_calls=100,
    )
"""

from __future__ import annotations

from typing import Any, Callable


def _content_to_text(content: Any) -> str:
    """Flatten a langchain message ``.content`` to plain text.

    ``.content`` is a ``str`` for most providers but a list of content
    blocks (bare strings, or dicts carrying a ``"text"`` key) for
    Anthropic-style multi-part responses. gepa wants a single string.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text") or block.get("content") or ""
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return str(content)


def chat_model_as_reflection_lm(model: Any) -> Callable[[str], str]:
    """Adapt a langchain ``BaseChatModel`` to a gepa ``reflection_lm``.

    gepa calls ``reflection_lm(prompt)`` and expects the model's text
    completion back. Invoke the chat model with the prompt as a single
    user turn and return its flattened text content.
    """

    def _reflection_lm(prompt: str) -> str:
        response = model.invoke(prompt)
        return _content_to_text(getattr(response, "content", response))

    return _reflection_lm


def reflection_lm_from_config(config: Any | None = None) -> Callable[[str], str]:
    """Build a gepa ``reflection_lm`` from mimir's configured model.

    Reuses :func:`mimir.agent.resolve_model_from_config` so the reflection
    LM is the same provider/model the agent already runs on — no extra
    credentials, and no separate hand-threaded Config field mapping. Pass an
    explicit ``config`` to override; defaults to
    :meth:`mimir.config.Config.from_env`. Imports are lazy to avoid a
    circular import at module load and to keep importing this module cheap.
    """
    from .agent import resolve_model_from_config
    from .config import Config

    cfg = config if config is not None else Config.from_env()
    model = resolve_model_from_config(cfg)
    return chat_model_as_reflection_lm(model)
