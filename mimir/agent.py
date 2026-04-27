"""Claude Agent SDK driver (SPEC §4.2, §9.3, §5.6).

Run-turn flow:
1. ``session_manager.touch(channel_id)`` — ensure an active MSAM session,
   reset its idle timer, attach ``msam_session_id`` to the TurnContext.
2. Append inbound to chat_history.jsonl + deques.
3. Flush any pending INDEX.md rebuilds.
4. Pre-message MSAM hook (skipped on ``trigger="msam_session_end"``):
   query MSAM, format hits into the turn prompt, stash atom_ids.
5. Build system + turn prompts. The synthesis turn uses a special template.
6. Set the ``contextvars`` TurnContext so MSAM tools can auto-credit.
7. Invoke ``query()``, collect messages, extract events.
8. Append outbound to chat_history.jsonl.
9. Post-message MSAM hook (skipped on ``trigger="msam_session_end"``):
   call ``mark_contributions`` with the union of pre-injected and
   mid-turn-queried atom_ids, scoped to the active session.
10. End-of-turn INDEX.md rebuild (debounced, SPEC §3.4).
11. Write the turns.jsonl record.

The TurnContext is the only mutable per-turn state. Subagents run in distinct
asyncio tasks → distinct ContextVar copies → they do NOT inherit the parent's
``msam_atom_ids`` (SPEC §9.3, verified).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from claude_agent_sdk import (
    ClaudeAgentOptions,
    HookMatcher,
    ResultMessage,
    TaskNotificationMessage,
    TaskStartedMessage,
    query,
)

from . import _context
from .channel_registry import ChannelRegistry
from .config import Config
from .event_logger import log_event
from .history import MessageBuffer
from .hooks import make_post_tool_use_hook, make_pre_tool_use_hook
from .index import IndexGenerator
from .loop_detector import LoopDetector
from .memory import load_core
from .models import AgentEvent, TurnContext, TurnRecord, make_turn_id
from .msam_client import MsamClient, MsamError
from .msamtools import _atom_ids_from_response, _atoms_in_payload, _format_atoms
from .prompts import build_system_prompt, build_turn_prompt
from .scheduler import Scheduler
from .search import Indexer
from .session_manager import SessionManager
from .subagent_inbox import SubagentInbox, SubagentResult, render_subagent_updates
from .templates import render_msam_session_end
from .tools import SDK_PRESET_TOOLS, allowed_tool_names, build_mcp_server
from .turn_logger import TurnLogger, extract_turn_events, truncate_input

log = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _filter_session_turns(turns_path, msam_session_id: str) -> list[dict]:
    """Read turns.jsonl and return all records with the given msam_session_id."""
    if not turns_path.is_file():
        return []
    out: list[dict] = []
    try:
        with turns_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("msam_session_id") == msam_session_id:
                    out.append(rec)
    except OSError:
        return []
    return out


class Agent:
    def __init__(
        self,
        config: Config,
        turn_logger: TurnLogger,
        message_buffer: MessageBuffer,
        index_generator: IndexGenerator,
        indexer: Indexer | None = None,
        msam_client: MsamClient | None = None,
        session_manager: SessionManager | None = None,
        scheduler: Scheduler | None = None,
        subagent_inbox: SubagentInbox | None = None,
        channel_registry: ChannelRegistry | None = None,
    ) -> None:
        self._config = config
        self._turn_logger = turn_logger
        self._buffer = message_buffer
        self._indexes = index_generator
        self._indexer = indexer
        self._msam = msam_client
        self._sessions = session_manager
        self._scheduler = scheduler
        self._inbox = subagent_inbox or SubagentInbox()
        self._channels = channel_registry

        self._mcp_server = build_mcp_server(
            config.home,
            indexer=indexer,
            msam_client=msam_client,
            scheduler=scheduler,
            channel_registry=channel_registry,
        )

        # Hooks layer mimir's path confinement + post-write reindex onto the
        # SDK preset tools (Read/Write/Edit/Bash/Glob).
        async def _reindex(rel: str) -> None:
            if self._indexer is not None:
                await self._indexer.reindex_path(rel)

        self._pre_tool_hook = make_pre_tool_use_hook(config.home)
        self._post_tool_hook = make_post_tool_use_hook(
            config.home, _reindex if indexer is not None else None
        )

    def _build_options(self, system_prompt: str) -> ClaudeAgentOptions:
        effort = self._config.effort
        if effort not in ("low", "medium", "high", "max"):
            effort = "high"
        return ClaudeAgentOptions(
            system_prompt=system_prompt,
            tools=list(SDK_PRESET_TOOLS),
            mcp_servers={"mimir": self._mcp_server},
            allowed_tools=allowed_tool_names(
                include_search=self._indexer is not None,
                include_msam=self._msam is not None,
                include_scheduler=self._scheduler is not None,
                include_channels=self._channels is not None,
            ),
            permission_mode="bypassPermissions",
            hooks={
                "PreToolUse": [
                    HookMatcher(
                        # MultiEdit / NotebookEdit kept in the regex so the
                        # path-confinement hook still fires if either becomes
                        # available later; dropping them costs nothing today.
                        matcher="Read|Write|Edit|MultiEdit|Glob|Grep|NotebookEdit",
                        hooks=[self._pre_tool_hook],
                    )
                ],
                "PostToolUse": [
                    HookMatcher(
                        matcher="Write|Edit|MultiEdit",
                        hooks=[self._post_tool_hook],
                    )
                ],
            },
            model=self._config.model,
            effort=effort,
            thinking={"type": "adaptive"},
            env=self._config.sdk_env_overrides(),
            cwd=str(self._config.home),
            include_partial_messages=False,
        )

    # ---- chat history --------------------------------------------------

    async def _record_inbound(self, event: AgentEvent) -> None:
        if not event.content or event.trigger == "msam_session_end":
            return
        kind = "user_message" if event.trigger == "user_message" else "system_note"
        msg = self._buffer.make_message(
            channel_id=event.channel_id,
            kind=kind,
            content=event.content,
            author=event.author,
            author_display=event.author,
            msg_id=event.source_id,
            source=event.source,
        )
        await self._buffer.append(msg)

    async def _record_outbound(
        self, channel_id: str, output: str, *, source: str | None = None
    ) -> None:
        if not output:
            return
        msg = self._buffer.make_message(
            channel_id=channel_id,
            kind="assistant_message",
            content=output,
            source=source,
        )
        await self._buffer.append(msg)

    # ---- MSAM hooks ----------------------------------------------------

    async def _pre_message_hook(self, ctx: TurnContext, event: AgentEvent) -> str | None:
        """Query MSAM, stash atom_ids on ctx, return a formatted prompt block
        (or None if nothing relevant). Skipped on synthesis turns.

        Floors the per-atom confidence tier at the configured threshold
        (default "medium") because auto-fetched atoms cost system-prompt
        budget every turn — low-confidence noise here is net-negative."""
        if self._msam is None or ctx.trigger == "msam_session_end":
            return None
        if not event.content:
            return None
        min_tier = (self._config.msam_pre_message_min_tier or "").strip() or None
        try:
            payload = await self._msam.query(
                event.content,
                top_k=12,
                session_id=ctx.msam_session_id,
                min_confidence_tier=min_tier,
            )
        except MsamError as exc:
            await log_event(
                "msam_query_error",
                where="pre_message_hook",
                error=str(exc),
                turn_id=ctx.turn_id,
            )
            return None
        ids = _atom_ids_from_response(payload)
        if not ids:
            return None
        seen = set(ctx.msam_atom_ids)
        for aid in ids:
            if aid not in seen:
                ctx.msam_atom_ids.append(aid)
                seen.add(aid)
        hits = _atoms_in_payload(payload)
        return _format_atoms(hits)

    async def _post_message_hook(self, ctx: TurnContext, output: str) -> None:
        """Credit pre-injected ∪ mid-turn-queried atoms via mark_contributions.

        Fallback path: ``send_message`` is the primary credit hook (it
        carries the actual delivered text — see channeltools.py). This hook
        only fires when the turn produced no send_message (e.g. scheduled
        ticks that wrote to memory but didn't reply, or background work).
        Skipped on synthesis turns (the agent already called msam_feedback
        per atom in step 2 of the synthesis prompt)."""
        if self._msam is None or ctx.trigger == "msam_session_end":
            return
        if ctx.send_message_count > 0:
            # send_message already credited the atoms with the real reply.
            return
        if not ctx.msam_atom_ids or not output:
            return
        try:
            await self._msam.feedback(
                list(dict.fromkeys(ctx.msam_atom_ids)),  # de-dup, preserve order
                output,
                session_id=ctx.msam_session_id,
            )
        except MsamError as exc:
            await log_event(
                "msam_feedback_error",
                where="post_message_hook",
                error=str(exc),
                turn_id=ctx.turn_id,
            )

    # ---- synthesis turn ------------------------------------------------

    def _build_synthesis_prompt(self, ctx: TurnContext, event: AgentEvent) -> str:
        """For trigger='msam_session_end' — load the synthesis template,
        embed the session's turn window from turns.jsonl."""
        msam_session_id = ctx.msam_session_id or event.extra.get("msam_session_id", "")
        idle_minutes = self._config.msam_session_idle_minutes
        turns_window = _filter_session_turns(self._config.turns_log, msam_session_id)
        return render_msam_session_end(
            channel_id=event.channel_id,
            msam_session_id=msam_session_id,
            idle_minutes=idle_minutes,
            turns_window=turns_window,
            prompts_dir=self._config.prompts_dir,
        )

    # ---- run_turn ------------------------------------------------------

    async def run_turn(self, event: AgentEvent) -> TurnRecord:
        ctx = TurnContext(
            turn_id=make_turn_id(),
            session_id=event.channel_id,
            trigger=event.trigger,
            channel_id=event.channel_id,
            started_at=time.monotonic(),
            tool_call_budget=self._config.tool_call_budget,
            loop_detector=LoopDetector(
                soft_limit=self._config.send_loop_soft_limit,
                hard_limit=self._config.send_loop_hard_limit,
                similarity_threshold=self._config.send_loop_similarity,
            ),
        )

        # 1. MSAM session attach. Synthesis turns already carry the closed
        #    session's id; for everything else we touch (creating if needed).
        if event.trigger == "msam_session_end":
            ctx.msam_session_id = event.extra.get("msam_session_id")
        elif self._sessions is not None:
            session = await self._sessions.touch(event.channel_id)
            ctx.msam_session_id = session.msam_session_id
            self._sessions.increment_turn_count(event.channel_id)

        # 2. Inbound → chat_history (skipped for synthesis turns; their
        #    "input" is the turn-window block, not a real user message).
        await self._record_inbound(event)

        # 3. Flush any out-of-band INDEX changes before reading the index.
        await self._indexes.flush()

        # 4. Pre-message MSAM hook — produces a "Possibly relevant memories"
        #    block to slot into the turn prompt.
        msam_block = await self._pre_message_hook(ctx, event)

        # 4b. Drain any background-subagent notifications that landed since
        #     the last turn for this channel (SPEC §4.4).
        pending_subagents = await self._inbox.drain(event.channel_id)
        subagent_block = (
            render_subagent_updates(pending_subagents) if pending_subagents else None
        )

        # 5. Build prompts.
        if event.trigger == "msam_session_end":
            turn_prompt = self._build_synthesis_prompt(ctx, event)
            recent: list = []
        else:
            recent = self._buffer.assemble_recent_activity(
                channel_id=event.channel_id,
                author=event.author,
                recent_per_channel=self._config.recent_per_channel,
                recent_author_cross=self._config.recent_author_cross,
                cross_hours=self._config.recent_cross_hours,
                source_allowlist=self._config.recent_sources,
            )
            turn_prompt = build_turn_prompt(
                event,
                recent_messages=recent,
                msam_block=msam_block,
                subagent_block=subagent_block,
                recent_message_chars=self._config.recent_message_chars,
            )

        core_blocks = load_core(self._config.home)
        memory_index_body = self._indexes.read_memory_index()
        system_prompt = build_system_prompt(
            core_blocks=core_blocks,
            memory_index_body=memory_index_body,
        )

        await log_event(
            "turn_started",
            turn_id=ctx.turn_id,
            channel_id=ctx.channel_id,
            trigger=ctx.trigger,
            msam_session_id=ctx.msam_session_id,
            core_block_count=len(core_blocks),
            recent_message_count=len(recent),
            msam_atoms_pre_injected=len(ctx.msam_atom_ids),
        )

        # 6. Set TurnContext on the contextvar so MSAM tools auto-credit.
        token = _context.set_current_turn(ctx)
        messages: list = []
        error: str | None = None
        try:
            try:
                async for msg in query(prompt=turn_prompt, options=self._build_options(system_prompt)):
                    messages.append(msg)
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"
                log.exception("query() failed for turn %s", ctx.turn_id)
        finally:
            _context.reset_current_turn(token)

        events_list, output = extract_turn_events(messages)
        duration_ms = int((time.monotonic() - ctx.started_at) * 1000)

        # 7a. ResultMessage capture (Phase 8 — resume detection + cost).
        #     The SDK emits one ResultMessage per turn at end-of-stream. We
        #     keep the last one in case retries land more than one. None
        #     when query() crashed before emitting any.
        result_msg: ResultMessage | None = None
        for msg in messages:
            if isinstance(msg, ResultMessage):
                result_msg = msg

        # 7b. Subagent notification side-channel (SPEC §4.4). The SDK yields
        #     ``TaskStartedMessage`` / ``TaskNotificationMessage`` on the parent
        #     stream — we collect descriptions when the task starts so we can
        #     attach them to the eventual completion notification.
        task_descriptions: dict[str, str] = {}
        for msg in messages:
            if isinstance(msg, TaskStartedMessage):
                task_descriptions[msg.task_id] = msg.description
        for msg in messages:
            if isinstance(msg, TaskNotificationMessage):
                await self._inbox.push(
                    event.channel_id,
                    SubagentResult(
                        task_id=msg.task_id,
                        status=msg.status,
                        summary=msg.summary,
                        output_file=msg.output_file,
                        description=task_descriptions.get(msg.task_id),
                        usage=msg.usage,
                        received_ts=_utc_now_iso(),
                    ),
                )
                await log_event(
                    "subagent_notification",
                    turn_id=ctx.turn_id,
                    channel_id=event.channel_id,
                    task_id=msg.task_id,
                    status=msg.status,
                )

        # 8. Outbound → chat_history (skip for synthesis turn — there's no
        #    user-facing message; the prompt instructs the agent not to send).
        #    Outbound inherits the inbound's source so the assistant reply
        #    participates in Recent activity rendering on the same allowlist
        #    as the human turn (open-strix-style).
        if output and event.trigger != "msam_session_end":
            await self._record_outbound(
                event.channel_id, output, source=event.source
            )

        # 9. Post-message MSAM hook.
        if not error:
            await self._post_message_hook(ctx, output)

        # 10. End-of-turn INDEX rebuild (debounced).
        self._indexes.mark_dirty("all")
        await self._indexes.flush()

        record = TurnRecord(
            ts=_utc_now_iso(),
            turn_id=ctx.turn_id,
            session_id=ctx.session_id,
            msam_session_id=ctx.msam_session_id,
            trigger=ctx.trigger,
            channel_id=ctx.channel_id,
            input=truncate_input(turn_prompt),
            msam_atom_ids=list(dict.fromkeys(ctx.msam_atom_ids)),
            events=events_list,
            output=output,
            duration_ms=duration_ms,
            error=error,
            result_subtype=result_msg.subtype if result_msg else None,
            result_is_error=result_msg.is_error if result_msg else None,
            stop_reason=result_msg.stop_reason if result_msg else None,
            num_turns=result_msg.num_turns if result_msg else None,
            total_cost_usd=result_msg.total_cost_usd if result_msg else None,
            usage=result_msg.usage if result_msg else None,
            permission_denials=list(result_msg.permission_denials or []) if result_msg else [],
        )
        await self._turn_logger.write(record)

        await log_event(
            "turn_finished",
            turn_id=ctx.turn_id,
            channel_id=ctx.channel_id,
            duration_ms=duration_ms,
            error=error,
            result_subtype=result_msg.subtype if result_msg else None,
            result_is_error=result_msg.is_error if result_msg else None,
            total_cost_usd=result_msg.total_cost_usd if result_msg else None,
            event_count=len(events_list),
            output_chars=len(output),
            msam_atoms_total=len(record.msam_atom_ids),
        )
        return record
