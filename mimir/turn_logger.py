"""turns.jsonl writer + LangChain message → events extractor.

Walks a list of ``langchain_core.messages`` (AIMessage, ToolMessage,
HumanMessage) and produces:
  - a list of events (``reasoning``, ``tool_call``, ``tool_result``)
  - a final-output string (assistant text not associated with tool use)
  - SDK-equivalent result fields (cost / usage / stop_reason /
    num_turns) derived from ``response_metadata`` and ``usage_metadata``

The schema is unchanged from the SDK era — bench tooling
(``benchmark/scripts/collate_turns.py``, ``benchmark/overview_turns.py``,
the turn viewer) reads this output without modification.

Three message shapes are supported:
  - ``AIMessage.tool_calls`` (langchain-anthropic, langchain-openai)
  - ``AIMessage.response_metadata["internal_tool_calls"]`` +
    ``["tool_results"]`` (ChatClaudeCode / Max OAuth subprocess)
  - ``ToolMessage`` (standard LangGraph tool-call roundtrip)
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from ._jsonl_tail import _tail_lines, count_lines_chunked
from .models import TurnRecord

log = logging.getLogger(__name__)

MAX_TOOL_RESULT_BYTES = 4 * 1024
MAX_INPUT_BYTES = 2 * 1024
DEFAULT_MAX_TURNS = 1000


def truncate_input(prompt: str) -> str:
    if len(prompt) <= MAX_INPUT_BYTES:
        return prompt
    return prompt[:MAX_INPUT_BYTES] + "…[truncated]"


#: Content-block ``type`` values that carry user-visible reply text.
#: Anything outside this set is intentionally dropped by
#: ``_coerce_content``. ``None`` is in the set so legacy / pre-typed
#: blocks (``{"text": "..."}`` with no ``type`` key) still pass through.
_VISIBLE_TEXT_BLOCK_TYPES: frozenset[str | None] = frozenset({"text", None})


def _extract_thinking_blocks(content: Any) -> list[str]:
    """Walk a LangChain message content list and return the text of
    Anthropic-style structured thinking blocks (Anthropic extended
    thinking + Minimax / Moonshot Kimi via Anthropic-compat).

    Returns a list of reasoning texts in document order. Empty list
    for string content or content without any thinking blocks.

    Use case: ``extract_turn_events`` emits each captured block as a
    ``reasoning`` event with ``source: "model_thinking_block"`` so
    ``turns.jsonl`` preserves the model's chain of thought for
    introspection / debugging — even though ``_coerce_content``
    correctly drops the same blocks from the user-visible output
    path.

    ``redacted_thinking`` blocks (Anthropic's tamper-evident
    server-encrypted form) are captured with a placeholder marker —
    we record that a redacted block was present without the
    content, since the ``data`` field is opaque base64.
    """
    if not isinstance(content, list):
        return []
    out: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "thinking":
            text = item.get("thinking")
            if isinstance(text, str) and text:
                out.append(text)
        elif item_type == "redacted_thinking":
            out.append("[redacted thinking block — server-encrypted]")
    return out


def _coerce_content(content: Any) -> str:
    """Flatten a LangChain message content field into a single string
    that's safe to render to user-facing surfaces (bridge.send,
    messages.jsonl).

    LangChain returns content as either a plain string (legacy shape)
    or a list of structured content blocks. The block types we know
    about:

    * ``{"type": "text", "text": "..."}`` — user-visible reply. **KEPT.**
    * ``{"type": "thinking", "thinking": "...", "signature": "..."}`` —
      Anthropic extended-thinking reasoning (and Minimax via
      Anthropic-compat). **DROPPED** — not user-visible; the agent's
      private chain of thought.
    * ``{"type": "redacted_thinking", "data": "..."}`` — Anthropic's
      tamper-evident-but-server-redacted reasoning form. **DROPPED.**
    * ``{"type": "tool_use", …}`` / ``{"type": "tool_result", …}`` —
      tool roundtrip structure. **DROPPED** here because
      ``extract_turn_events`` extracts these separately via the
      tool_calls / ToolMessage paths; including them again as text
      would double-count.

    Items with no ``type`` key fall back to ``item.get("text")`` for
    back-compat with the legacy LangChain shape.

    **Pre-fix behavior (2026-05-20 muninn-mimir cutover):** unknown
    types fell back to ``json.dumps(item)``, which dumped the raw
    thinking block (with its ``signature`` cryptographic field and
    reasoning text) as JSON into the user-visible reply. Discord
    showed Muninn's private reasoning as a literal JSON dict above
    every response. The fix is the explicit type filter.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                item_type = item.get("type")
                if item_type in _VISIBLE_TEXT_BLOCK_TYPES:
                    text = item.get("text")
                    if isinstance(text, str) and text:
                        parts.append(text)
                # else: thinking / redacted_thinking / tool_use /
                # tool_result / any future-unknown typed block —
                # silently skipped (not user-visible).
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


def extract_turn_events(
    messages: list[Any],
    *,
    streaming_active: bool = False,    # kept for API compat; ignored
    message_t_ms: list[float] | None = None,  # kept for API compat; ignored
) -> tuple[list[dict[str, Any]], str]:
    """Walk a LangChain message list, return ``(events, output)``.

    Schema:
      AIMessage with content + tool_calls  → reasoning + tool_call events
                                              (UNLESS it's the final
                                              AIMessage of the turn —
                                              then content is output AND
                                              tool_calls still emit)
      AIMessage with only content          → appended to output
      ToolMessage                          → tool_result event

    Why the "final AIMessage is output even with tool_calls" rule:
    when the model runs through ChatClaudeCode, the claude subprocess
    executes tools INSIDE the subprocess and returns ONE AIMessage with
    {final answer text, internal_tool_calls, internal_tool_results}. If
    we treat that as pure reasoning, ``output`` ends up empty even
    though the agent actually answered. Bench adapters poll ``output``
    for the canonical reply; without this rule every turn looks like a
    no-op. Pre-181-P regression: the bluesky_recall bench scored
    near-zero because every probe's ``output`` field was blank — the
    answer text was sitting in a reasoning event.

    streaming_active + message_t_ms are accepted for back-compat
    with the SDK version's call sites; ignored in this build.
    """
    # Find the index of the final AIMessage so we can promote its
    # content to ``output`` even when it carries tool_calls. Other
    # AIMessages with content + tool_calls remain reasoning.
    last_ai_idx = -1
    for i, msg in enumerate(messages):
        if isinstance(msg, AIMessage):
            last_ai_idx = i

    events: list[dict[str, Any]] = []
    output_parts: list[str] = []
    for i, msg in enumerate(messages):
        if isinstance(msg, AIMessage):
            # Capture Anthropic-style structured thinking blocks BEFORE
            # ``_coerce_content`` drops them. Each block becomes its own
            # ``reasoning`` event with ``source: "model_thinking_block"``
            # so turns.jsonl preserves the model's chain of thought even
            # though the user-visible output path strips it. Ordering:
            # emit reasoning events first (chronologically the model
            # thinks then replies), then the rest of the legacy
            # content/tool-call logic.
            for _think_text in _extract_thinking_blocks(msg.content):
                events.append({
                    "type": "reasoning",
                    "source": "model_thinking_block",
                    "content": _think_text,
                })
            content_text = _coerce_content(msg.content)
            # ChatClaudeCode executes tools inside the ``claude`` CLI
            # subprocess and stashes the parsed ToolUseBlocks under
            # ``response_metadata["internal_tool_calls"]`` (NOT on
            # ``msg.tool_calls``, to keep LangGraph from re-executing
            # them). Tool results land under one of two keys depending
            # on how the message reached us:
            #   - ``"tool_results"``           — non-streaming
            #     ``_generate`` path (``ChatClaudeCode.invoke``).
            #   - ``"internal_tool_results"``  — streaming ``_stream``
            #     path (``ChatClaudeCode.astream``), which is what
            #     ``Agent._run_turn_body`` uses via ``agent.astream``.
            # Read both so we capture results regardless of provider /
            # call mode. Pre-fix the streaming path silently dropped
            # results from every claude-code built-in tool
            # (Bash/Read/Edit/Grep/Glob): only the LangGraph-native
            # ``ToolMessage`` path was captured, while the subprocess-
            # executed Bash/Read/Edit/Grep/Glob ToolResultBlocks
            # surfaced under ``internal_tool_results`` and we missed
            # them all. mimirbot's turn 24a1a8858209 (2026-05-17,
            # commitment-store-bug fix) recorded 63 tool_calls but
            # only 2 tool_results — every result captured was the
            # LangGraph-native path.
            rmd = getattr(msg, "response_metadata", None) or {}
            # Hooks-based capture path (preferred when present): the
            # ``install_tool_event_hooks`` patch in
            # ``_langchain_claude_code_patches.py`` registers SDK
            # PreToolUse/PostToolUse/PostToolUseFailure hooks that record
            # every tool invocation — built-in, bridged, MCP — into a
            # single ordered list paired by ``tool_use_id``. When that
            # list is present, walk it directly: events arrive in the
            # actual call→result→call→result order the model executed
            # (vs. the bunched call-list-then-result-list shape produced
            # by the legacy ``internal_tool_calls`` / ``internal_tool_results``
            # split). Falls through to the legacy path when the hooks
            # patch isn't loaded (e.g. anthropic-only operator).
            tool_events = rmd.get("tool_events")
            if tool_events:
                is_final_ai = i == last_ai_idx
                if content_text and not is_final_ai:
                    events.append({"type": "reasoning", "content": content_text})
                elif content_text:
                    output_parts.append(content_text)
                    events.append({"type": "reasoning", "content": content_text})
                for te in tool_events:
                    te_type = te.get("type")
                    if te_type == "tool_call":
                        events.append({
                            "type": "tool_call",
                            "id": te.get("tool_use_id", ""),
                            "name": te.get("name", "unknown"),
                            "args": te.get("input"),
                        })
                    elif te_type == "tool_result":
                        # Use ``is not None`` rather than truthiness so an
                        # empty-string result (e.g. a Bash call with no
                        # output) is preserved rather than falling through
                        # to the error field.
                        _te_result = te.get("result")
                        body = _coerce_content(
                            _te_result if _te_result is not None else te.get("error")
                        )
                        if len(body) > MAX_TOOL_RESULT_BYTES:
                            body = body[:MAX_TOOL_RESULT_BYTES] + "…[truncated]"
                        events.append({
                            "type": "tool_result",
                            "id": te.get("tool_use_id", ""),
                            "name": te.get("name", ""),
                            "content": body,
                            "is_error": bool(te.get("is_error")),
                        })
                continue  # AIMessage handled via tool_events; skip legacy path
            internal_tcs = rmd.get("internal_tool_calls") or []
            internal_trs = (
                rmd.get("internal_tool_results")
                or rmd.get("tool_results")
                or []
            )
            tcs = list(msg.tool_calls or []) + list(internal_tcs)
            # Build a tool_use_id → name lookup so tool_result events
            # can carry a usable ``name``. The records produced by
            # langchain-claude-code's ``_parse_assistant_message``
            # include ``tool_use_id``, ``content``, and ``is_error``
            # but NOT ``name`` — that has to come from the matching
            # tool_call's ``id``. Without this lookup, every captured
            # tool_result rendered with ``name=""`` even when the
            # streaming-keys fix above let them through.
            tc_name_by_id: dict[str, str] = {}
            for tc in tcs:
                tc_id = tc.get("id")
                tc_name = tc.get("name")
                if tc_id and tc_name:
                    tc_name_by_id[tc_id] = tc_name
            is_final_ai = i == last_ai_idx
            if content_text and tcs and not is_final_ai:
                # Intermediate "thinking out loud" between tool calls.
                events.append({"type": "reasoning", "content": content_text})
            elif content_text:
                # Either a text-only AIMessage OR the final AIMessage
                # (whose content is the agent's answer even if it also
                # carries internal_tool_calls).
                output_parts.append(content_text)
                if tcs:
                    # Still record the reasoning trace alongside output
                    # so turn_viewer can render the model's pre-tool
                    # commentary on the final AIMessage. The output
                    # field carries the user-visible reply; this gives
                    # operators the full picture in the turn log.
                    events.append({"type": "reasoning", "content": content_text})
            for tc in tcs:
                events.append({
                    "type": "tool_call",
                    "id": tc.get("id", ""),
                    "name": tc.get("name", "unknown"),
                    "args": tc.get("args") or tc.get("input"),
                })
            for tr in internal_trs:
                # Use ``is not None`` rather than truthiness — empty-string
                # content (a tool that returns "") is a valid result and
                # must not fall through to the ``result`` fallback field.
                _tr_content = tr.get("content")
                body = _coerce_content(
                    _tr_content if _tr_content is not None else tr.get("result")
                )
                if len(body) > MAX_TOOL_RESULT_BYTES:
                    body = body[:MAX_TOOL_RESULT_BYTES] + "…[truncated]"
                tr_id = tr.get("tool_use_id", "") or ""
                # Reverse-lookup name via the tool_use_id ↔ tool_call.id
                # match; fall back to the record's own ``name`` (the
                # non-streaming key shape used to include it) if the
                # lookup misses.
                tr_name = tc_name_by_id.get(tr_id) or tr.get("name", "") or ""
                events.append({
                    "type": "tool_result",
                    "id": tr_id,
                    "name": tr_name,
                    "content": body,
                    "is_error": bool(tr.get("is_error")),
                })
        elif isinstance(msg, ToolMessage):
            body = _coerce_content(msg.content)
            if len(body) > MAX_TOOL_RESULT_BYTES:
                body = body[:MAX_TOOL_RESULT_BYTES] + "…[truncated]"
            events.append({
                "type": "tool_result",
                "id": getattr(msg, "tool_call_id", ""),
                "name": getattr(msg, "name", ""),
                "content": body,
                "is_error": getattr(msg, "status", None) == "error",
            })
    return events, "\n".join(output_parts)


def derive_result_fields(messages: list[Any]) -> dict[str, Any]:
    """Pull SDK-equivalent ResultMessage fields from LangChain messages.

    LangChain stores stop_reason in response_metadata, usage in
    usage_metadata. langchain-anthropic / langchain-openai populate
    these; some providers populate them inconsistently. Returns None
    for fields we can't resolve — matches mimir's existing nullable
    contract for these fields.
    """
    final_ai: AIMessage | None = None
    for msg in messages:
        if isinstance(msg, AIMessage):
            final_ai = msg

    if final_ai is None:
        return {
            "result_subtype": None,
            "result_is_error": None,
            "stop_reason": None,
            "num_turns": None,
            "total_cost_usd": None,
            "usage": None,
        }

    md = final_ai.response_metadata or {}
    stop_reason = md.get("stop_reason") or md.get("finish_reason")

    # Aggregate usage_metadata across all AI messages.
    agg_in = agg_out = agg_cache_read = agg_cache_create = 0
    has_usage = False
    for msg in messages:
        if isinstance(msg, AIMessage) and msg.usage_metadata:
            has_usage = True
            u = msg.usage_metadata
            agg_in += u.get("input_tokens", 0)
            agg_out += u.get("output_tokens", 0)
            details = u.get("input_token_details", {}) or {}
            agg_cache_read += details.get("cache_read", 0)
            agg_cache_create += details.get("cache_creation", 0)
    usage = None
    if has_usage:
        usage = {
            "input_tokens": agg_in,
            "output_tokens": agg_out,
            "cache_read_input_tokens": agg_cache_read,
            "cache_creation_input_tokens": agg_cache_create,
        }

    # ChatClaudeCode is the LangChain provider that wraps the Claude
    # Code CLI subprocess. It mirrors the CLI's final ResultMessage
    # into ``response_metadata`` — pick up the cost, turn count,
    # usage, and is_error signals it surfaces there. The native
    # langchain providers (anthropic / openai) populate
    # ``usage_metadata`` instead, handled above.
    #
    # Streaming-path note (``ChatClaudeCode._astream``): the upstream
    # code DROPS ``stop_reason`` / ``num_turns`` / ``is_error`` from
    # generation_info, emitting only a binary ``finish_reason``
    # (``"stop"`` / ``"error"``). ``enrich_streaming_metadata()`` in
    # ``_langchain_claude_code_patches`` patches that to preserve the
    # original fields, so production reads work normally. The
    # fallbacks below are defense-in-depth for deployments where the
    # patch didn't apply (claude-code extra absent, upstream version
    # incompatible with the wrapper, etc.).
    cc_usage = md.get("usage")
    if usage is None and cc_usage:
        usage = cc_usage
    cc_num_turns = md.get("num_turns")
    cc_total_cost = md.get("total_cost_usd")
    cc_is_error = md.get("is_error")
    if cc_is_error is None:
        # Streaming path collapses ``msg.is_error`` into
        # ``finish_reason``. Recover the binary signal — granular
        # error categories are lost without the patch above.
        fr = md.get("finish_reason")
        if fr == "error":
            cc_is_error = True
        elif fr == "stop":
            cc_is_error = False

    # num_turns fallback chain:
    #   1. ``response_metadata["num_turns"]`` — populated by
    #      ``ChatClaudeCode`` (both call modes; non-streaming directly
    #      and streaming via ``enrich_streaming_metadata``'s patch).
    #      This is the SDK's per-request model-turn count.
    #   2. ``count(AIMessage in messages)`` — fallback for native
    #      providers (langchain-anthropic / -openai) which don't emit
    #      ``num_turns`` in response_metadata. Counts how many model
    #      invocations produced this turn — close-enough proxy for
    #      "internal turns" since each tool-call cycle yields one
    #      AIMessage chunk and the final reply yields one more.
    #   3. ``None`` when there are no AIMessages at all (empty turn /
    #      error before any model response).
    num_turns = cc_num_turns if cc_num_turns is not None else (
        sum(1 for m in messages if isinstance(m, AIMessage)) or None
    )
    result_subtype = "success"
    result_is_error = bool(cc_is_error) if cc_is_error is not None else False
    # Truncation reasons across all model providers — model ran out of
    # budget mid-response. Provider-specific names:
    #   - claude-code SDK: ``"max_turns"`` (per-request loop cap) +
    #     ``"max_tokens"`` (per-response token cap)
    #   - langchain-anthropic native: ``"max_tokens"``
    #   - langchain-openai native: ``"length"`` (the canonical OpenAI
    #     finish_reason for max-tokens truncation)
    # All land in ``result_subtype="error_max_turns"`` — the name is
    # SDK-era legacy; the semantic is "model hit a budget cap and
    # the response is truncated."
    if stop_reason in ("max_turns", "max_tokens", "length"):
        result_subtype = "error_max_turns"
        result_is_error = True

    return {
        "result_subtype": result_subtype,
        "result_is_error": result_is_error,
        "stop_reason": stop_reason,
        "num_turns": num_turns,
        "total_cost_usd": cc_total_cost,
        "usage": usage,
    }


class TurnLogger:
    """Append-only JSONL with bounded retention. Lock-serialized writes.

    Identical surface to the SDK-side TurnLogger — same TurnRecord
    schema, same _jsonl_tail helpers. The only change is the
    underlying event extractor lives in this module instead of
    consuming SDK message types.
    """

    def __init__(self, path: Path, max_turns: int = DEFAULT_MAX_TURNS) -> None:
        self._path = path
        self._max_turns = max(1, max_turns)
        self._line_count = 0
        self._lock: asyncio.Lock | None = None
        path.parent.mkdir(parents=True, exist_ok=True)
        self._line_count = count_lines_chunked(path)
        # Monotonic turn counter. The newest retained record carries the
        # high-water mark (trimming keeps the newest), so seed from it; if the
        # records predate ``seq``, backfill them once so the count is populated.
        self._seq = self._seed_seq()

    def _seed_seq(self) -> int:
        try:
            for line in _tail_lines(self._path):  # newest-first
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except ValueError:
                    break  # torn newest line — fall through to backfill
                if isinstance(obj, dict) and isinstance(obj.get("seq"), int):
                    return obj["seq"]
                break  # newest record has no seq — backfill all records
        except OSError:
            return 0  # no file yet
        return self._backfill_seq()

    def _backfill_seq(self) -> int:
        """Assign ``seq`` (1..N) to existing records that lack it, once."""
        try:
            raw = self._path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return 0
        out: list[str] = []
        seq = 0
        for line in raw:
            stripped = line.strip()
            if not stripped:
                continue
            seq += 1
            try:
                obj = json.loads(stripped)
            except ValueError:
                out.append(stripped)  # keep torn lines verbatim
                continue
            if isinstance(obj, dict):
                obj.setdefault("seq", seq)
                out.append(json.dumps(obj, ensure_ascii=True, default=str))
            else:
                out.append(stripped)
        if out:
            try:
                tmp = self._path.with_suffix(".jsonl.tmp")
                tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
                tmp.rename(self._path)
            except OSError as exc:
                log.warning("Failed to backfill turn seq: %s", exc)
        return seq

    async def write(self, record: TurnRecord) -> None:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            await self._write(record)

    async def _write(self, record: TurnRecord) -> None:
        self._seq += 1
        record.seq = self._seq
        line = json.dumps(asdict(record), ensure_ascii=True, default=str)
        try:
            await asyncio.to_thread(self._append_line, line)
            self._line_count += 1
            # Hysteresis: trim only when over cap by ≥10%.
            if self._line_count > int(self._max_turns * 1.1):
                await self._trim()
        except OSError as exc:
            log.warning("Failed to write turn record: %s", exc)

    def _append_line(self, line: str) -> None:
        with self._path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    async def _trim(self) -> None:
        await asyncio.to_thread(self._trim_sync)

    def _trim_sync(self) -> None:
        # ``_tail_lines`` is a single-arg, newest-first generator (it was
        # rewritten to stream from the file tail in 8 KiB chunks). The
        # prior call here passed ``self._max_turns`` as a second positional
        # arg and treated the result as a bounded chronological list —
        # both wrong post-rewrite: the 2-arg call raised ``TypeError``
        # (NOT caught by the ``except OSError`` below, so it crashed the
        # turn-write coroutine), and even without that it would have
        # written the whole file reversed. Mirror ``EventLogger._trim_sync``:
        # take the newest ``_max_turns`` lines, then reverse for a
        # chronological rewrite.
        kept_reversed: list[str] = []
        try:
            for line in _tail_lines(self._path):
                stripped = line.strip()
                if not stripped:
                    continue
                kept_reversed.append(stripped)
                if len(kept_reversed) >= self._max_turns:
                    break
        except OSError as exc:
            log.warning("Failed to trim turn log: %s", exc)
            return
        if not kept_reversed:
            return
        # tail yields newest-first; reverse for chronological rewrite.
        kept = list(reversed(kept_reversed))
        tmp = self._path.with_suffix(".jsonl.tmp")
        tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
        tmp.rename(self._path)
        self._line_count = len(kept)


def make_turn_id() -> str:
    import uuid
    return uuid.uuid4().hex[:12]
