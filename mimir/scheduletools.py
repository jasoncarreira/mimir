"""Scheduler-facing MCP tools (SPEC §7.5).

Three tools matching open-strix's semantics — atomic add-or-replace by name,
list, remove. All mutations serialize through ``Scheduler._mutate_lock`` so
two concurrent ``add_schedule`` calls with the same name produce a sane file
(last-writer-wins on the YAML, but the scheduler reload still picks the most
recent registration).

There is no ``edit_schedule`` — call ``add_schedule`` with the same ``name``
to replace.
"""

from __future__ import annotations

from typing import Any

import yaml
from claude_agent_sdk import SdkMcpTool, tool

from ._tool_helpers import _content_block, _need, _safe
from .scheduler import Scheduler, SchedulerJob


def build_schedule_tools(scheduler: Scheduler) -> list[SdkMcpTool]:
    @tool(
        "list_schedules",
        "List all currently registered schedule jobs as YAML.",
        {},
    )
    @_safe("list_schedules")
    async def list_schedules(args: dict[str, Any]) -> dict[str, Any]:
        jobs = await scheduler.list_jobs()
        if not jobs:
            return _content_block("(no schedules)")
        body = yaml.safe_dump(
            [j.to_yaml_entry() for j in jobs],
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
        )
        return _content_block(body)

    @tool(
        "add_schedule",
        "Add or replace a schedule by name. Exactly one of cron (5-field) "
        "or time_of_day (HH:MM, daily UTC) must be set. Provide EITHER "
        "``prompt`` (inline text), ``prompt_file`` (path under "
        "<home>/prompts/, e.g. ``daily-review.md``), OR ``callable`` "
        "(name of a code-side-registered non-LLM cron callable like "
        "``saga-consolidate`` or ``identities-populate``). Per-cron "
        "prompt files are the preferred shape for LLM ticks. The "
        "``callable`` field overrides the env-var-default cron of a "
        "registered callable; pass an empty cron to disable a callable "
        "for this deployment. channel_id is optional — if omitted, the "
        "tick fires on a synthetic scheduler:<name> channel "
        "(LLM-tick entries only; callable entries don't carry "
        "channel_id). Replaces any existing job with the same name.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "prompt": {"type": "string"},
                "prompt_file": {"type": "string"},
                "callable": {"type": "string"},
                "cron": {"type": "string"},
                "time_of_day": {"type": "string"},
                "channel_id": {"type": "string"},
            },
            "required": ["name"],
        },
    )
    @_safe("add_schedule")
    async def add_schedule(args: dict[str, Any]) -> dict[str, Any]:
        name = _need(args, "name")
        prompt = (args.get("prompt") or "").strip()
        prompt_file = (args.get("prompt_file") or "").strip() or None
        callable_name = (args.get("callable") or "").strip() or None
        cron = (args.get("cron") or "").strip() or None
        time_of_day = (args.get("time_of_day") or "").strip() or None
        channel_id = args.get("channel_id")
        if isinstance(channel_id, str):
            channel_id = channel_id.strip() or None
        kind_count = sum(bool(x) for x in (prompt, prompt_file, callable_name))
        if kind_count == 0:
            return _content_block(
                "add_schedule failed: one of 'prompt', 'prompt_file', "
                "or 'callable' required",
                is_error=True,
            )
        if kind_count > 1:
            return _content_block(
                "add_schedule failed: 'prompt', 'prompt_file', and "
                "'callable' are mutually exclusive — exactly one",
                is_error=True,
            )
        if callable_name is not None:
            # Callable entries: cron only (time_of_day is for LLM ticks).
            # Empty cron is the explicit-disable signal — allowed.
            if time_of_day:
                return _content_block(
                    "add_schedule failed: callable entries use 'cron' "
                    "only; 'time_of_day' is for prompt entries",
                    is_error=True,
                )
            # Validate against the registry early so a bad name fails
            # with a clear list of options rather than a generic
            # 'not registered' message after writing yaml.
            registered = scheduler.registered_callables()
            if callable_name not in registered:
                return _content_block(
                    f"add_schedule failed: callable {callable_name!r} "
                    f"is not registered. Available: {registered!r}",
                    is_error=True,
                )
        else:
            if bool(cron) == bool(time_of_day):
                return _content_block(
                    "add_schedule failed: exactly one of cron or time_of_day required",
                    is_error=True,
                )
        job = SchedulerJob(
            name=name,
            prompt=prompt,
            prompt_file=prompt_file,
            callable_name=callable_name,
            cron=cron,
            time_of_day=time_of_day,
            channel_id=channel_id,
        )
        try:
            await scheduler.add_job(job)
        except ValueError as exc:
            return _content_block(f"add_schedule failed: {exc}", is_error=True)
        return _content_block(f"add_schedule ok: {name}")

    @tool(
        "remove_schedule",
        "Remove a schedule by name. Returns false if no schedule with that "
        "name exists.",
        {"name": str},
    )
    @_safe("remove_schedule")
    async def remove_schedule(args: dict[str, Any]) -> dict[str, Any]:
        name = _need(args, "name")
        removed = await scheduler.remove_job(name)
        if not removed:
            return _content_block(f"remove_schedule: no job named {name!r}")
        return _content_block(f"remove_schedule ok: {name}")

    @tool(
        "reload_pollers",
        "Re-scan ``<home>/.claude/skills/**/pollers.json`` and "
        "(re-)register any pollers found. Use after installing a new "
        "skill that drops a ``pollers.json`` file, so its pollers go "
        "live without a container restart. Returns the count of "
        "pollers registered.",
        {},
    )
    @_safe("reload_pollers")
    async def reload_pollers(args: dict[str, Any]) -> dict[str, Any]:
        # PR #141 review item #1+2: ``count`` now reflects the live
        # total (preserved + freshly-installed), matching the names
        # list. ``invalid_events`` (chainlink #84) is non-empty when
        # one or more ``pollers.json`` failed to JSON-parse this
        # reload — surface a warning inline so the operator who
        # triggered this tool doesn't have to scan events.jsonl to
        # learn the reload partially failed.
        count = await scheduler.reload_pollers()
        names = scheduler.registered_pollers()
        invalid_events = scheduler.last_invalid_manifest_events()
        if count == 0 and not invalid_events:
            return _content_block(
                "reload_pollers: 0 pollers registered (no "
                "<home>/.claude/skills/**/pollers.json files found, "
                "or skills_dir not wired)."
            )
        body = (
            f"reload_pollers ok: {count} poller(s) registered — "
            f"{', '.join(names)}"
        )
        if invalid_events:
            n = len(invalid_events)
            preserved = sorted(
                {
                    name
                    for ev in invalid_events
                    for name in ev.get("preserved_pollers", [])
                }
            )
            preserved_clause = (
                f"; preserved {len(preserved)} prior poller"
                f"{'s' if len(preserved) != 1 else ''}"
                f" ({', '.join(preserved)})"
                if preserved
                else ""
            )
            body += (
                f" (warning: {n} manifest"
                f"{'s' if n != 1 else ''} failed to parse"
                f"{preserved_clause} — see events.jsonl for paths)"
            )
        return _content_block(body)

    return [list_schedules, add_schedule, remove_schedule, reload_pollers]


def schedule_tool_names() -> list[str]:
    return [
        "mcp__mimir__list_schedules",
        "mcp__mimir__add_schedule",
        "mcp__mimir__remove_schedule",
        "mcp__mimir__reload_pollers",
    ]
