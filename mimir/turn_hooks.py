"""Per-turn lifecycle hooks for ``Agent.run_turn`` (CR#15).

``run_turn`` used to be a 460-line linear function with 11 numbered
phases. Most phases are core to the turn (build prompt, run SDK query,
extract output, log the record), but a handful are *extensible* —
behaviors that future feedback loops or §12 follow-ons want to layer on
without editing the orchestrator. Those behaviors live here as
``TurnLifecycleHook`` implementations registered into a
``list[TurnLifecycleHook]`` on Agent.

**This is NOT the SDK's PreToolUse / PostToolUse hook system.** Those
fire per-tool-call from inside the SDK's control task; ``TurnLifecycleHook``
fires at four turn-level seams from inside ``Agent.run_turn`` on the
agent's task. Different protocol, different lifecycle. Naming is
deliberate: avoid ``TurnHook`` (would be confusable with the tool hook).

Seams:

- ``pre_query``  — after the prompt is built and TurnContext is
  registered, before the SDK ``query()`` loop runs. Use for any
  per-turn state reset on the hook itself (most hooks no-op here).
- ``on_message`` — once per message yielded by the SDK loop. Used by
  the rate-limit observer and the subagent-lifecycle observer; both
  filter on isinstance for the message types they care about.
- ``post_query`` — after the loop completes and ``output``/``error``
  are extracted, before the TurnRecord is written. Used for plan-quota
  capture (needs ``options``) and the post-message saga hook.
- ``finalize``   — after the TurnRecord is written + ``turn_finished``
  event emitted. Used for index rebuild, git commit, cancel-typing.

Hooks fire in registration order at each seam; ordering is explicit in
``Agent._build_turn_hooks``. Exceptions raised by a hook are caught and
logged by the orchestrator — a misbehaving hook can't sink the turn.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from .event_logger import log_event

if TYPE_CHECKING:
    from claude_agent_sdk import ClaudeAgentOptions

    from .channel_registry import ChannelRegistry
    from .indexes import IndexGenerator
    from .models import AgentEvent, TurnContext, TurnRecord
    from .rate_limits import RateLimitStore
    from .subagent_inbox import SubagentInbox

log = logging.getLogger(__name__)


class TurnLifecycleHook:
    """Per-turn lifecycle hook. Override the methods you care about;
    defaults are no-ops.

    Hooks share state via the ``TurnContext`` (per-turn, mutable). They
    must NOT store per-turn state on ``self`` — multi-channel concurrent
    turns share hook instances and would corrupt each other. Per-hook
    config (constructor args) is fine; per-turn state goes on ctx.
    """

    name: str = "unnamed"

    async def pre_query(
        self, ctx: "TurnContext", event: "AgentEvent",
    ) -> None:
        """Reset/setup before the SDK query loop runs. Default: no-op."""
        return None

    async def on_message(
        self, ctx: "TurnContext", event: "AgentEvent", msg: Any,
    ) -> None:
        """Observe a single SDK message. Filter by ``isinstance`` for
        the types you care about. Default: no-op."""
        return None

    async def post_query(
        self,
        ctx: "TurnContext",
        event: "AgentEvent",
        *,
        messages: list,
        output: str,
        error: str | None,
        options: "ClaudeAgentOptions",
    ) -> None:
        """Run after the query loop completes. Default: no-op."""
        return None

    async def finalize(
        self, ctx: "TurnContext", event: "AgentEvent", record: "TurnRecord",
    ) -> None:
        """Run after the TurnRecord is written + ``turn_finished``
        event emitted. Default: no-op."""
        return None


# ─── Concrete hooks ──────────────────────────────────────────────────


class RateLimitObserverHook(TurnLifecycleHook):
    """Records ``RateLimitEvent`` and ``StreamEvent.message_start``
    rate-limit headers into the ``RateLimitStore`` and emits warning /
    rejection events for the agent's algedonic feedback path.

    StreamEvent path is gated off under Claude Max OAuth (the OAuth
    poller is the value-writer in that mode; the response headers
    don't carry per-window utilization). See the comment block in the
    body for the full rationale — this is a verbatim port of run_turn's
    7a dispatch walk's RateLimitEvent + StreamEvent branches.
    """

    name = "rate_limit_observer"

    def __init__(
        self,
        rate_limits: "RateLimitStore",
        is_max_oauth: Callable[[], bool],
    ) -> None:
        self._rate_limits = rate_limits
        self._is_max_oauth = is_max_oauth

    async def on_message(self, ctx, event, msg):
        from claude_agent_sdk import RateLimitEvent, StreamEvent

        from .rate_limits import (
            snapshot_from_response_bucket,
            snapshot_from_sdk_event,
        )

        if isinstance(msg, RateLimitEvent):
            info = msg.rate_limit_info
            rl_type = getattr(info, "rate_limit_type", None)
            if not rl_type:
                return
            try:
                await self._rate_limits.record(
                    rl_type, snapshot_from_sdk_event(info),
                )
            except Exception:  # noqa: BLE001
                log.exception("rate_limits.record failed for %s", rl_type)
            if info.status in ("allowed_warning", "rejected"):
                await log_event(
                    "rate_limit_warning"
                    if info.status == "allowed_warning"
                    else "rate_limit_rejected",
                    rate_limit_type=rl_type,
                    utilization=info.utilization,
                    resets_at=info.resets_at,
                )
        elif isinstance(msg, StreamEvent):
            # Max OAuth gate — see module-level docstring + run_turn's
            # original 7a comment block. Under OAuth, response headers
            # don't carry per-window utilization%; the OAuth poller is
            # the real-value writer. Direct-API-key deployments are the
            # inverse: the poller is gated off and StreamEvent headers
            # carry the real values.
            if self._is_max_oauth():
                return
            ev = msg.event or {}
            if ev.get("type") != "message_start":
                return
            api_message = ev.get("message") or {}
            rate_limits = api_message.get("rate_limits")
            if not isinstance(rate_limits, dict):
                return
            for bucket_type, bucket in rate_limits.items():
                if not isinstance(bucket, dict):
                    continue
                try:
                    await self._rate_limits.record(
                        bucket_type,
                        snapshot_from_response_bucket(bucket),
                    )
                except Exception:  # noqa: BLE001
                    log.exception(
                        "rate_limits.record from response failed for %s",
                        bucket_type,
                    )


class SubagentLifecycleHook(TurnLifecycleHook):
    """Handles ``TaskStartedMessage`` / ``TaskProgressMessage`` /
    ``TaskNotificationMessage`` events from background subagents:
    emits ``subagent_*`` events to events.jsonl and pushes
    notifications onto the per-channel subagent inbox.

    Per-turn ``task_descriptions`` mapping (task_id → description) is
    stored on ``ctx`` (NOT on the hook) so concurrent turns on
    different channels don't interfere. The SDK guarantees
    ``TaskStarted`` fires before any ``TaskProgress`` /
    ``TaskNotification`` for the same task_id, so the description is
    populated by the time downstream messages fire.
    """

    name = "subagent_lifecycle"

    def __init__(self, inbox: "SubagentInbox") -> None:
        self._inbox = inbox
        # CR2 (agent runtime) fix: process-level task description
        # registry. Pre-fix, ``ctx.task_descriptions`` lived for one
        # turn — but background-spawned subagents (most per
        # ``subagent_defs.py``: ``background=True``) complete in a
        # later turn whose ctx has an empty dict. The notification
        # lookup at ``ctx.task_descriptions.get(msg.task_id)`` then
        # returned None, and the inbox push carried ``description=None``
        # — so the ``Subagent updates`` prompt block rendered
        # ``[completed] task_id=abc — None`` instead of the actual
        # task description.
        #
        # Capacity bound: subagent task_ids are 12-char hashes; even
        # a year of heavy use stays well under 10k. Capping at 4096
        # keeps memory bounded while leaving headroom; eviction is
        # FIFO (oldest task_id evicted first).
        from collections import OrderedDict
        self._task_descriptions: OrderedDict[str, str] = OrderedDict()

    _TASK_DESC_LRU_CAP = 4096

    def _record_task_description(self, task_id: str, description: str) -> None:
        if task_id in self._task_descriptions:
            self._task_descriptions.move_to_end(task_id)
        self._task_descriptions[task_id] = description
        while len(self._task_descriptions) > self._TASK_DESC_LRU_CAP:
            self._task_descriptions.popitem(last=False)

    async def on_message(self, ctx, event, msg):
        from claude_agent_sdk import (
            TaskNotificationMessage,
            TaskProgressMessage,
            TaskStartedMessage,
        )

        from .subagent_inbox import SubagentResult

        if isinstance(msg, TaskStartedMessage):
            # Keep the per-turn dict for back-compat (callers reading
            # ``ctx.task_descriptions`` directly still work) AND the
            # process-level registry for cross-turn lookup.
            ctx.task_descriptions[msg.task_id] = msg.description
            self._record_task_description(msg.task_id, msg.description)
            await log_event(
                "subagent_started",
                turn_id=ctx.turn_id,
                channel_id=event.channel_id,
                task_id=msg.task_id,
                description=msg.description,
                task_type=getattr(msg, "task_type", None),
            )
        elif isinstance(msg, TaskProgressMessage):
            u = msg.usage or {}
            await log_event(
                "subagent_progress",
                turn_id=ctx.turn_id,
                channel_id=event.channel_id,
                task_id=msg.task_id,
                description=msg.description,
                last_tool_name=getattr(msg, "last_tool_name", None),
                total_tokens=u.get("total_tokens"),
                tool_uses=u.get("tool_uses"),
                duration_ms=u.get("duration_ms"),
            )
        elif isinstance(msg, TaskNotificationMessage):
            from .agent import _utc_now_iso  # avoid module-load cycle
            # Look up description: per-turn dict first (matches the
            # original semantics for same-turn completions), then the
            # process-level registry (for background subagents that
            # complete in a later turn — the original bug).
            description = (
                ctx.task_descriptions.get(msg.task_id)
                or self._task_descriptions.get(msg.task_id)
            )
            await self._inbox.push(
                event.channel_id,
                SubagentResult(
                    task_id=msg.task_id,
                    status=msg.status,
                    summary=msg.summary,
                    output_file=msg.output_file,
                    description=description,
                    usage=msg.usage,
                    received_ts=_utc_now_iso(),
                ),
            )
            u = msg.usage or {}
            await log_event(
                "subagent_notification",
                turn_id=ctx.turn_id,
                channel_id=event.channel_id,
                task_id=msg.task_id,
                status=msg.status,
                total_tokens=u.get("total_tokens"),
                tool_uses=u.get("tool_uses"),
                duration_ms=u.get("duration_ms"),
            )


class PlanQuotaCaptureHook(TurnLifecycleHook):
    """Plan-window apiUsage capture (Stage 5 of
    CLAUDE_SDK_CLIENT_MIGRATION.md). Skipped when the message loop
    crashed; the next successful turn picks it up.

    Needs the per-turn ``options`` so the underlying client lookup
    matches the warm pooled client. Receives it via the ``post_query``
    kwargs the orchestrator passes through.
    """

    name = "plan_quota_capture"

    def __init__(
        self,
        capture_fn: Callable[["ClaudeAgentOptions"], Awaitable[None]],
    ) -> None:
        self._capture_fn = capture_fn

    async def post_query(
        self, ctx, event, *, messages, output, error, options,
    ):
        if error:
            return
        try:
            await self._capture_fn(options)
        except Exception:  # noqa: BLE001
            log.exception("_capture_plan_quota_from_client raised")


class PostMessageSagaHook(TurnLifecycleHook):
    """Calls Agent's ``_post_message_hook`` after a successful turn —
    saga ``mark_contributions`` + saga ``end_session`` synthesis-flag
    audit. Skipped on error so we don't credit / audit broken turns.
    """

    name = "post_message_saga"

    def __init__(
        self,
        hook_fn: Callable[["TurnContext", str], Awaitable[None]],
    ) -> None:
        self._hook_fn = hook_fn

    async def post_query(
        self, ctx, event, *, messages, output, error, options,
    ):
        if error:
            return
        await self._hook_fn(ctx, output)


class IndexRebuildHook(TurnLifecycleHook):
    """End-of-turn debounced INDEX rebuild. ``mark_dirty("all")``
    queues the rebuild; ``flush()`` runs it if the debounce window has
    elapsed (otherwise the next dirty-mark + flush cycle picks it up)."""

    name = "index_rebuild"

    def __init__(self, indexes: "IndexGenerator") -> None:
        self._indexes = indexes

    async def finalize(self, ctx, event, record):
        self._indexes.mark_dirty("all")
        await self._indexes.flush()


_WIKI_GENERATED_OUTPUTS = frozenset({
    "orphans.md",
    "dangling-links.md",
    "backlinks-index.md",
})


class WikiBacklinksHook(TurnLifecycleHook):
    """Regenerate ``state/wiki/{orphans,dangling-links,backlinks-index}.md``
    when any *content* page under ``state/wiki/`` was modified during this
    turn. Runs the same code as ``mimir wiki backlinks``, so the algedonic
    ``wiki_backlinks_unhealthy`` event surfaces orphan/dangling regressions
    on the turn that introduced them — no operator-discipline dependency,
    no periodic-cron latency.

    Edit-triggered, not periodic: the failure modes (orphans, dangling
    links) are caused by wiki *writes*, not by the passage of time.
    Running on a schedule would lag the failure by up to the schedule
    interval; running on every turn unconditionally would write the 3
    generated outputs every turn (constant diff churn). The mtime
    snapshot threads that needle — only run when at least one
    non-generated wiki page changed mtime relative to the snapshot.

    Detection works via a pre_query → finalize mtime diff: pre_query
    records ``{path: mtime}`` for every wiki page; finalize re-stats
    and looks for any page whose mtime moved (or any new/removed
    page). Both reads are wall-clock ``st_mtime``, so no clock-domain
    mismatch (``ctx.started_at`` is ``time.monotonic()`` and is NOT
    safe to compare against ``st_mtime`` directly — that comparison
    would always return True on a process running for more than the
    Unix epoch in monotonic seconds, which is "always" in practice).

    Runs **before** ``IndexRebuildHook`` so ``state/INDEX.md`` reflects
    the freshly-regenerated outputs on the same turn (rather than
    lagging by one turn). Runs **before** ``GitCommitHook`` so the 3
    regenerated outputs are part of the same git commit as the writes
    that triggered them.

    Loop-safety: the 3 generated outputs land at the wiki *root*
    (``state/wiki/orphans.md`` etc.); IndexRebuildHook's outputs
    (``memory/INDEX.md``, ``state/INDEX.md``, ``state/wiki/index.md``)
    are either outside ``state/wiki/`` or in the wiki backlinks
    ``_META_FILENAMES`` exclusion set, so neither writes back into
    files this hook tracks.
    """

    name = "wiki_backlinks"

    def __init__(self, home) -> None:
        self._home = home

    def _snapshot_mtimes(self) -> dict[str, float]:
        """Walk the wiki, return ``{absolute_path_str: st_mtime}`` for
        every non-generated content page. Uses absolute path as the
        key so finalize's lookup matches even if cwd shifts.
        Empty dict when the wiki dir doesn't exist."""
        wiki = self._home / "state" / "wiki"
        snapshot: dict[str, float] = {}
        if not wiki.is_dir():
            return snapshot
        for page in wiki.rglob("*.md"):
            if page.name in _WIKI_GENERATED_OUTPUTS:
                continue
            try:
                snapshot[str(page)] = page.stat().st_mtime
            except OSError:
                continue
        return snapshot

    async def pre_query(self, ctx, event):
        # Snapshot mtimes BEFORE the SDK loop runs. Stored on ctx (NOT
        # on self) so concurrent turns on different channels don't
        # share state — the multi-channel-correctness invariant from
        # CR#15.
        ctx.wiki_mtime_snapshot = self._snapshot_mtimes()

    async def finalize(self, ctx, event, record):
        wiki = self._home / "state" / "wiki"
        if not wiki.is_dir():
            return
        before: dict[str, float] = getattr(ctx, "wiki_mtime_snapshot", {})
        after = self._snapshot_mtimes()

        # Touched if any of:
        #   - new page added (in `after` but not `before`)
        #   - existing page mtime changed
        #   - page removed (in `before` but not `after`)
        # Iteration short-circuits on first hit.
        touched = False
        for path_str, mtime in after.items():
            if before.get(path_str) != mtime:
                touched = True
                break
        if not touched:
            for path_str in before:
                if path_str not in after:
                    touched = True
                    break
        if not touched:
            return

        from . import wiki_backlinks
        try:
            await wiki_backlinks.run(self._home)
        except FileNotFoundError:
            # Wiki dir disappeared between the snapshot and run() —
            # benign race; nothing to regenerate.
            return


class GitCommitHook(TurnLifecycleHook):
    """PR 4a: post-turn git commit + debounced push (gated on
    ``MIMIR_GIT_TRACKING_ENABLED``). Runs after the index rebuild so
    auto-regenerated INDEX.md files are part of the same commit as the
    writes that triggered them.

    Failures are swallowed inside ``commit_turn_changes`` and surfaced
    via ``git_commit_failed`` / ``git_push_failed`` algedonic events.
    """

    name = "git_commit"

    def __init__(self, home, enabled: bool) -> None:
        self._home = home
        self._enabled = enabled

    async def finalize(self, ctx, event, record):
        from . import git_tracking

        await git_tracking.commit_turn_changes(
            turn_id=ctx.turn_id,
            trigger=ctx.trigger,
            home=self._home,
            enabled=self._enabled,
        )


class CancelTypingHook(TurnLifecycleHook):
    """Cancel any typing indicator the bridge spawned on inbound.
    ``send()`` already cancels typing on the destination channel for
    normal replies; this handles the edge cases where ``send()`` never
    landed on the inbound channel — cross-channel-only sends, errored
    turns, heartbeat-shaped flow that explicitly went silent.
    """

    name = "cancel_typing"

    def __init__(self, channels: "ChannelRegistry | None") -> None:
        self._channels = channels

    async def finalize(self, ctx, event, record):
        if self._channels is None or not ctx.channel_id:
            return
        bridge = self._channels.find(ctx.channel_id)
        if bridge is None:
            return
        try:
            await bridge.cancel_typing(ctx.channel_id)
        except Exception:  # noqa: BLE001
            # Typing is best-effort. Don't let a stray exception mask
            # the actual turn record we're about to return.
            log.debug("cancel_typing(%s) failed", ctx.channel_id)


# ─── Orchestrator helpers ────────────────────────────────────────────


async def fire_pre_query(
    hooks: list[TurnLifecycleHook], ctx, event,
) -> None:
    for hook in hooks:
        try:
            await hook.pre_query(ctx, event)
        except Exception:  # noqa: BLE001
            log.exception("turn-hook %s pre_query raised", hook.name)


async def fire_on_message(
    hooks: list[TurnLifecycleHook], ctx, event, msg,
) -> None:
    for hook in hooks:
        try:
            await hook.on_message(ctx, event, msg)
        except Exception:  # noqa: BLE001
            log.exception("turn-hook %s on_message raised", hook.name)


async def fire_post_query(
    hooks: list[TurnLifecycleHook],
    ctx,
    event,
    *,
    messages,
    output,
    error,
    options,
) -> None:
    for hook in hooks:
        try:
            await hook.post_query(
                ctx, event,
                messages=messages, output=output, error=error,
                options=options,
            )
        except Exception:  # noqa: BLE001
            log.exception("turn-hook %s post_query raised", hook.name)


async def fire_finalize(
    hooks: list[TurnLifecycleHook], ctx, event, record,
) -> None:
    for hook in hooks:
        try:
            await hook.finalize(ctx, event, record)
        except Exception:  # noqa: BLE001
            log.exception("turn-hook %s finalize raised", hook.name)
