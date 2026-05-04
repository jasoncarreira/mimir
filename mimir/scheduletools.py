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
        "``prompt`` (inline text) OR ``prompt_file`` (path under "
        "<home>/prompts/, e.g. ``daily-review.md``). Per-cron prompt "
        "files are the preferred shape — they keep prompt content out "
        "of scheduler.yaml so it can grow without cluttering the "
        "registration. channel_id is optional — if omitted, the tick "
        "fires on a synthetic scheduler:<name> channel. Replaces any "
        "existing job with the same name.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "prompt": {"type": "string"},
                "prompt_file": {"type": "string"},
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
        cron = (args.get("cron") or "").strip() or None
        time_of_day = (args.get("time_of_day") or "").strip() or None
        channel_id = args.get("channel_id")
        if isinstance(channel_id, str):
            channel_id = channel_id.strip() or None
        if not prompt and not prompt_file:
            return _content_block(
                "add_schedule failed: one of 'prompt' or 'prompt_file' required",
                is_error=True,
            )
        if bool(cron) == bool(time_of_day):
            return _content_block(
                "add_schedule failed: exactly one of cron or time_of_day required",
                is_error=True,
            )
        job = SchedulerJob(
            name=name,
            prompt=prompt,
            prompt_file=prompt_file,
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

    return [list_schedules, add_schedule, remove_schedule]


def schedule_tool_names() -> list[str]:
    return [
        "mcp__mimir__list_schedules",
        "mcp__mimir__add_schedule",
        "mcp__mimir__remove_schedule",
    ]
