"""``send_message`` + ``react`` MCP tools (SPEC §7.1, §7.2.4).

Both dispatch through the ``ChannelRegistry`` based on ``channel_id`` prefix.
``send_message`` is wrapped by the per-turn ``LoopDetector`` (SPEC §7.2.4):
near-duplicate spam triggers a soft warning at ``MIMIR_SEND_LOOP_SOFT_LIMIT``
and a hard refusal at ``MIMIR_SEND_LOOP_HARD_LIMIT``.

Defaults derive from the active ``TurnContext`` (via the contextvar) — the
turn's ``channel_id`` becomes the default ``channel_id``, and the agent's
loop detector is read from the context. Subagents inherit a fresh contextvar
copy so their sends use their own (typically inert) detector.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool

from ._context import get_current_turn
from ._tool_helpers import _content_block, _need, _safe
from .bridges._attachments import AttachmentPathError, resolve_outbound_path
from .bridges._directives import (
    ReactDirective,
    SendFileDirective,
    SendMessageDirective,
    parse_directives,
)
from .channel_registry import ChannelRegistry, UnknownChannelError
from .event_logger import log_event
from .history import MessageBuffer
from .loop_detector import (
    ERROR_REACTION,
    WARNING_REACTION,
    BreakerVerdict,
    LoopDetector,
)
from .saga_client import SagaClient, SagaError

log = logging.getLogger(__name__)


def _format_directive_summary(
    *,
    main_sent: bool,
    main_chunks: int,
    main_msg_id: str | None,
    results: list[dict[str, Any]],
) -> str:
    """Build the send_message tool reply string. Includes a per-directive
    bullet list when the call carried ``<actions>`` so the agent sees
    which directives succeeded vs failed before its next turn."""
    head = (
        f"send_message complete (sent={main_sent}, chunks={main_chunks}, "
        f"message_id={main_msg_id})"
    )
    if not results:
        return head
    lines = [head, "directives:"]
    for r in results:
        kind = r.get("kind", "?")
        ok = r.get("ok", False)
        flag = "ok" if ok else "FAIL"
        detail_bits: list[str] = []
        for k in ("emoji", "path", "channel_id"):
            if r.get(k):
                detail_bits.append(f"{k}={r[k]}")
        if not ok and r.get("error"):
            detail_bits.append(f"error={r['error']}")
        lines.append(f"  - {kind} [{flag}] " + ", ".join(detail_bits))
    return "\n".join(lines)


async def _dispatch_action_directives(
    registry: ChannelRegistry,
    *,
    fallback_channel_id: str,
    directives: tuple,
    default_message_id: str | None,
    outbound_root: Path | None,
) -> list[dict[str, Any]]:
    """Dispatch the parsed directives in source order. Returns one summary
    dict per directive for the audit log; never raises. Per-directive
    failures (path escape, unknown channel, bridge error) are recorded
    on the summary as ``ok=False`` with a reason — the agent sees the
    aggregated outcome on the send_message tool reply.
    """
    results: list[dict[str, Any]] = []
    for d in directives:
        if isinstance(d, ReactDirective):
            target_channel = (d.channel_id or fallback_channel_id).strip()
            target_msg = (d.message_id or default_message_id or "").strip()
            if not target_msg:
                results.append({
                    "kind": "react",
                    "ok": False,
                    "error": "no message_id and no recent message",
                    "emoji": d.emoji,
                })
                continue
            try:
                ok = await registry.react(target_channel, target_msg, d.emoji)
            except UnknownChannelError as exc:
                results.append({
                    "kind": "react", "ok": False,
                    "error": str(exc), "emoji": d.emoji,
                })
                continue
            except Exception as exc:  # noqa: BLE001
                log.exception("directive react failed")
                results.append({
                    "kind": "react", "ok": False,
                    "error": f"{type(exc).__name__}: {exc}", "emoji": d.emoji,
                })
                continue
            results.append({
                "kind": "react", "ok": bool(ok), "emoji": d.emoji,
                "channel_id": target_channel, "message_id": target_msg,
            })

        elif isinstance(d, SendFileDirective):
            target_channel = (d.channel_id or fallback_channel_id).strip()
            if outbound_root is None:
                results.append({
                    "kind": "send-file", "ok": False,
                    "error": "outbound attachments dir not configured",
                    "path": d.path,
                })
                continue
            try:
                resolved = resolve_outbound_path(outbound_root, d.path)
            except AttachmentPathError as exc:
                results.append({
                    "kind": "send-file", "ok": False,
                    "error": str(exc), "path": d.path,
                })
                continue
            caption = d.caption or ""
            try:
                send_res = await registry.send(
                    target_channel, caption, attachment_paths=[resolved],
                )
            except UnknownChannelError as exc:
                results.append({
                    "kind": "send-file", "ok": False,
                    "error": str(exc), "path": d.path,
                })
                continue
            except Exception as exc:  # noqa: BLE001
                log.exception("directive send-file failed")
                results.append({
                    "kind": "send-file", "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "path": d.path,
                })
                continue
            if d.cleanup and send_res.sent:
                try:
                    resolved.unlink(missing_ok=True)
                except OSError:
                    log.warning(
                        "directive send-file: cleanup of %s failed", resolved,
                    )
            results.append({
                "kind": "send-file", "ok": send_res.sent,
                "path": str(resolved), "channel_id": target_channel,
                "error": send_res.error,
            })

        elif isinstance(d, SendMessageDirective):
            target_channel = d.channel_id.strip()
            if not target_channel:
                results.append({
                    "kind": "send-message", "ok": False,
                    "error": "missing channel attribute",
                })
                continue
            try:
                send_res = await registry.send(target_channel, d.text)
            except UnknownChannelError as exc:
                results.append({
                    "kind": "send-message", "ok": False, "error": str(exc),
                    "channel_id": target_channel,
                })
                continue
            except Exception as exc:  # noqa: BLE001
                log.exception("directive send-message failed")
                results.append({
                    "kind": "send-message", "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "channel_id": target_channel,
                })
                continue
            results.append({
                "kind": "send-message", "ok": send_res.sent,
                "channel_id": target_channel,
                "error": send_res.error,
            })

    return results


def build_channel_tools(
    registry: ChannelRegistry,
    saga_client: SagaClient | None = None,
    message_buffer: MessageBuffer | None = None,
    home: Path | None = None,
) -> list[SdkMcpTool]:
    """Build the channel-aware send_message + react tools, closed over a
    shared ``ChannelRegistry``.

    When ``saga_client`` is provided, every successful ``send_message`` fires
    an SAGA ``mark_contributions`` pass (POST ``/v1/feedback``) with the
    sent text and the union of pre-injected + mid-turn-queried atom IDs in
    ``TurnContext.saga_atom_ids``. Crediting at send-time is more accurate
    than the agent-level ``_post_message_hook`` fallback because the
    delivered text is exactly what the user sees, whereas the SDK's "final
    output" can be empty (agent only used send_message) or include
    reasoning/scratch the user never receives. The post hook becomes a
    fallback for turns that don't ``send_message`` (scheduled ticks,
    bookkeeping turns).

    When ``message_buffer`` is provided, the delivered text is also written
    to chat_history as an ``assistant_message`` so the agent sees its own
    prior replies in the next turn's Recent activity. The agent-level
    ``_record_outbound`` is the fallback when no send_message fires."""

    @tool(
        "send_message",
        "Emit a message to a channel. If channel_id is omitted, uses the "
        "current turn's channel. Subject to a per-turn loop-detection "
        "circuit breaker — repeated near-duplicates first warn, then refuse. "
        "Returns 'send_message complete (sent=..., chunks=..., message_id=...)' "
        "or an is_error response on validation/breaker/dispatch failure.",
        # Explicit JSON schema so `channel_id` is optional — without this, the
        # SDK's dict-style schema marks every key required (claude_agent_sdk
        # __init__.py:_build_schema) and the MCP layer 422-rejects calls that
        # rely on the prompt's "channel_id defaults to the current turn"
        # guidance before our handler ever runs.
        {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "channel_id": {"type": "string"},
            },
            "required": ["text"],
        },
    )
    @_safe("send_message")
    async def send_message(args: dict[str, Any]) -> dict[str, Any]:
        text = args.get("text", "")
        if not isinstance(text, str):
            return _content_block("send_message failed: text must be a string", is_error=True)
        if not text.strip():
            return _content_block("send_message failed: text is empty", is_error=True)

        # Parse <actions>...</actions> directives out of the text. The
        # remaining clean_text is the user-visible message; directives
        # (react, send-file, send-message) get dispatched after the
        # main send. This collapses what would otherwise be 2-3 tool
        # calls (send_message + react + send_file) into one. If parsing
        # leaves clean_text empty but directives present, the main
        # send is skipped and only the directives fire.
        parsed = parse_directives(text)
        directives = parsed.directives
        text = parsed.clean_text
        if not text.strip() and not directives:
            return _content_block(
                "send_message failed: nothing to send (text became empty after stripping <actions> blocks)",
                is_error=True,
            )

        ctx = get_current_turn()
        channel_id = (args.get("channel_id") or "").strip()
        if not channel_id:
            channel_id = (ctx.channel_id if ctx else "") or ""
        if not channel_id:
            return _content_block(
                "send_message failed: no channel_id and no current turn channel",
                is_error=True,
            )

        # Outbound attachments root for <send-file path="..."> directives.
        # Computed once per call so AttachmentPathError surfaces as a
        # per-directive failure (not the whole tool call).
        outbound_root: Path | None = None
        if home is not None:
            outbound_root = home / "attachments" / "outbound"

        # When clean_text is empty, skip the main send + loop detector +
        # SAGA mark and go straight to directive dispatch. ctx's
        # last_assistant_message_id is the react-fallback target.
        if not text.strip():
            directive_results = await _dispatch_action_directives(
                registry,
                fallback_channel_id=channel_id,
                directives=directives,
                default_message_id=(ctx.last_assistant_message_id if ctx else None),
                outbound_root=outbound_root,
            )
            await log_event(
                "send_message",
                channel_id=channel_id,
                ok=True,
                chunks=0,
                message_id=None,
                text="",
                directives=directive_results,
            )
            return _content_block(
                _format_directive_summary(
                    main_sent=False, main_chunks=0, main_msg_id=None,
                    results=directive_results,
                )
            )

        # Loop-detection circuit breaker (SPEC §7.2.4).
        detector: LoopDetector | None = getattr(ctx, "loop_detector", None) if ctx else None
        if detector is not None:
            decision = detector.check(text)
            if decision.verdict == BreakerVerdict.HARD_STOP:
                # Refuse the send. Try to drop a ❌ on the channel for the
                # human watching — best-effort; ignore failures.
                try:
                    bridge = registry.find_or_raise(channel_id)
                    if ctx and ctx.last_assistant_message_id:
                        await bridge.react(channel_id, ctx.last_assistant_message_id, ERROR_REACTION)
                except UnknownChannelError:
                    pass
                except Exception:  # noqa: BLE001
                    log.exception("breaker hard-stop reaction failed")
                await log_event(
                    "send_message_loop_hard_stop",
                    channel_id=channel_id,
                    streak=decision.streak,
                    similarity=round(decision.similarity, 4),
                )
                return _content_block(
                    "send_message hard stop: repeated near-duplicate loop. "
                    "This send is refused. Reflect on what's wrong with the "
                    "approach before sending again — try a completely different "
                    "tactic or finish the turn.",
                    is_error=True,
                )
            if decision.verdict == BreakerVerdict.SOFT_WARN:
                if detector.mark_warning_emitted():
                    try:
                        bridge = registry.find_or_raise(channel_id)
                        if ctx and ctx.last_assistant_message_id:
                            await bridge.react(
                                channel_id, ctx.last_assistant_message_id, WARNING_REACTION
                            )
                    except UnknownChannelError:
                        pass
                    except Exception:  # noqa: BLE001
                        log.exception("breaker soft-warn reaction failed")
                await log_event(
                    "send_message_loop_warning",
                    channel_id=channel_id,
                    streak=decision.streak,
                    similarity=round(decision.similarity, 4),
                )
                # Soft warn doesn't refuse the send — fall through to dispatch.

        # Mark the attempt up-front so a failed dispatch (UnknownChannel,
        # bridge error, sent=False) still suppresses the agent-level
        # outbound fallback — otherwise the SDK's final assistant text
        # would be persisted to chat_history as if the user had received
        # it. Failures are visible in events.jsonl; chat_history shouldn't
        # claim a delivery that didn't happen.
        if ctx is not None:
            ctx.send_message_attempts += 1

        try:
            result = await registry.send(channel_id, text)
        except UnknownChannelError as exc:
            await log_event(
                "send_message_unknown_channel",
                channel_id=channel_id,
            )
            return _content_block(f"send_message failed: {exc}", is_error=True)
        except Exception as exc:  # noqa: BLE001
            log.exception("send_message dispatch failed")
            return _content_block(
                f"send_message failed: {type(exc).__name__}: {exc}",
                is_error=True,
            )

        if not result.sent:
            return _content_block(
                f"send_message failed: {result.error or 'bridge reported sent=False'}",
                is_error=True,
            )

        if ctx is not None and result.message_id:
            ctx.last_assistant_message_id = result.message_id

        # SAGA mark_contributions for the atoms in flight on this turn,
        # against the actual delivered text. Skipped on synthesis turns —
        # the agent calls saga_feedback per-atom there. Errors are logged
        # but never block the send (SAGA down ≠ user-visible failure).
        if (
            saga_client is not None
            and ctx is not None
            and ctx.trigger != "saga_session_end"
            and ctx.saga_atom_ids
        ):
            atom_ids_for_feedback = list(dict.fromkeys(ctx.saga_atom_ids))
            try:
                await saga_client.feedback(
                    atom_ids_for_feedback,  # de-dup, keep order
                    text,
                    session_id=ctx.saga_session_id,
                )
                await log_event(
                    "saga_feedback_sent",
                    where="send_message",
                    channel_id=channel_id,
                    n_atoms=len(atom_ids_for_feedback),
                    text_len=len(text),
                )
            except SagaError as exc:
                await log_event(
                    "saga_feedback_error",
                    where="send_message",
                    channel_id=channel_id,
                    error=str(exc),
                )

        if ctx is not None:
            ctx.send_message_count += 1

        # Persist the delivered text to chat_history as an assistant_message
        # so the agent sees its own prior reply in the next turn's Recent
        # activity. Source defaults to the inbound trigger's source when we
        # have a turn context; bridges that route by their own conventions
        # may pass through here even on outbound-only turns. agent.py's
        # _record_outbound (end of turn) is gated on send_message_count to
        # avoid duplicating this entry.
        if message_buffer is not None:
            try:
                msg = message_buffer.make_message(
                    channel_id=channel_id,
                    kind="assistant_message",
                    content=text,
                    msg_id=result.message_id,
                    source=ctx.channel_source if ctx is not None else None,
                )
                await message_buffer.append(msg)
            except Exception:  # noqa: BLE001
                log.exception("send_message: chat_history append failed")

        # Dispatch any <actions> directives the agent emitted alongside
        # the text (react / send-file / send-message). Directives run
        # AFTER the main send so reacts default to the just-sent
        # message id when no explicit ``message=`` was given. Per-
        # directive failures are recorded in the audit log; never
        # raise into the tool reply.
        directive_results = await _dispatch_action_directives(
            registry,
            fallback_channel_id=channel_id,
            directives=directives,
            default_message_id=result.message_id,
            outbound_root=outbound_root,
        )

        # Record the text on the event so adapters / the viewer can read what
        # was actually delivered. Cap to 4KB to keep events.jsonl small.
        text_for_log = text if len(text) <= 4096 else text[:4096] + "…[truncated]"
        await log_event(
            "send_message",
            channel_id=channel_id,
            ok=True,
            chunks=result.chunks,
            message_id=result.message_id,
            text=text_for_log,
            directives=directive_results if directive_results else None,
        )
        return _content_block(
            _format_directive_summary(
                main_sent=True, main_chunks=result.chunks,
                main_msg_id=result.message_id, results=directive_results,
            )
        )

    @tool(
        "react",
        "React to a message with an emoji. Defaults to the most recent "
        "assistant message on the current channel. Bridges that don't "
        "support native reactions (e.g. Bluesky) log a no-op.",
        {"emoji": str, "message_id": str, "channel_id": str},
    )
    @_safe("react")
    async def react(args: dict[str, Any]) -> dict[str, Any]:
        emoji = _need(args, "emoji")
        ctx = get_current_turn()
        channel_id = (args.get("channel_id") or "").strip()
        if not channel_id:
            channel_id = (ctx.channel_id if ctx else "") or ""
        if not channel_id:
            return _content_block(
                "react failed: no channel_id and no current turn channel",
                is_error=True,
            )

        message_id = (args.get("message_id") or "").strip()
        if not message_id and ctx is not None:
            message_id = ctx.last_assistant_message_id or ""
        if not message_id:
            return _content_block("react failed: no message_id and no recent message", is_error=True)

        try:
            ok = await registry.react(channel_id, message_id, emoji)
        except UnknownChannelError as exc:
            return _content_block(f"react failed: {exc}", is_error=True)
        except Exception as exc:  # noqa: BLE001
            log.exception("react dispatch failed")
            return _content_block(
                f"react failed: {type(exc).__name__}: {exc}",
                is_error=True,
            )
        await log_event(
            "react",
            channel_id=channel_id,
            message_id=message_id,
            emoji=emoji,
            ok=ok,
        )
        return _content_block(
            f"react ok={ok} (message_id={message_id}, emoji={emoji})"
        )

    @tool(
        "fetch_channel_history",
        "Fetch the last N messages from a channel — useful for catching "
        "up on a thread the bridge didn't see live (bot was offline, "
        "restart, mid-conversation join). Returns oldest-first. Pass "
        "``before`` (a message id from a prior fetch) to paginate further "
        "back. ``limit`` defaults to 20, capped at 100. Bridges without a "
        "history API (bench, web stub) return no messages.",
        {
            "type": "object",
            "properties": {
                "channel_id": {"type": "string"},
                "limit": {"type": "integer"},
                "before": {"type": "string"},
            },
            "required": [],
        },
    )
    @_safe("fetch_channel_history")
    async def fetch_channel_history(args: dict[str, Any]) -> dict[str, Any]:
        ctx = get_current_turn()
        channel_id = (args.get("channel_id") or "").strip()
        if not channel_id:
            channel_id = (ctx.channel_id if ctx else "") or ""
        if not channel_id:
            return _content_block(
                "fetch_channel_history failed: no channel_id and no current turn channel",
                is_error=True,
            )
        try:
            limit = max(1, min(int(args.get("limit") or 20), 100))
        except (TypeError, ValueError):
            limit = 20
        before = (args.get("before") or "").strip() or None

        try:
            messages = await registry.fetch_history(
                channel_id, limit=limit, before=before,
            )
        except UnknownChannelError as exc:
            return _content_block(
                f"fetch_channel_history failed: {exc}", is_error=True,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("fetch_channel_history dispatch failed")
            return _content_block(
                f"fetch_channel_history failed: {type(exc).__name__}: {exc}",
                is_error=True,
            )

        await log_event(
            "fetch_channel_history",
            channel_id=channel_id,
            limit=limit,
            before=before,
            returned=len(messages),
        )
        return _content_block(_format_history(channel_id, messages))

    return [send_message, react, fetch_channel_history]


def _format_history(channel_id: str, messages: list[Any]) -> str:
    """Render fetched messages as a compact prompt-friendly listing.
    One line per message: ``[ts msg=<id>] author: text [+N attachments]``.
    Empty list rendered as a single notice line."""
    if not messages:
        return f"fetch_channel_history: no messages returned for {channel_id}"
    lines = [f"fetch_channel_history: {len(messages)} messages from {channel_id}"]
    for m in messages:
        ts = getattr(m, "ts", "") or "?"
        mid = getattr(m, "id", "") or "?"
        author = (
            getattr(m, "author_display", None)
            or getattr(m, "author_id", None)
            or "?"
        )
        bot_tag = " (bot)" if getattr(m, "is_bot", False) else ""
        content = (getattr(m, "content", "") or "").replace("\n", " ")
        if len(content) > 500:
            content = content[:500] + "…"
        atts = getattr(m, "attachment_urls", ()) or ()
        att_tag = f" [+{len(atts)} attachments]" if atts else ""
        lines.append(f"[{ts} msg={mid}] {author}{bot_tag}: {content}{att_tag}")
    return "\n".join(lines)


def channel_tool_names() -> list[str]:
    return [
        "mcp__mimir__send_message",
        "mcp__mimir__react",
        "mcp__mimir__fetch_channel_history",
    ]
