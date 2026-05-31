"""Match a turn's channel_id to its corresponding skill, for auto-
surfacing the skill's SKILL.md content into the turn prompt.

Motivation
==========
Pollers wake the agent with a synthetic event on channel
``poller:<poller-name>``. The skills catalog lists every available
skill (name + description), but the agent has to actively reach for
``find-skills`` and load the matching skill to see its full
documentation. If the agent acts directly on the event without that
discovery step, it tends to default to globally-available tools
(``send_message``, ``Bash``) instead of the skill-specific workflow
(``outbox.yaml`` + ``social-cli dispatch``, for example).

Caught in production 2026-05-23: muninn-mimir saw a Bluesky feed
post it wanted to reply to. Instead of writing to
``outbox.yaml`` and running ``social-cli dispatch``, it used
``send_message`` — which routed the "reply" to a Discord channel.

This module is the codebase half of the fix: walk the available
skill directories, find any skill whose ``pollers.json`` declares
the poller name in the current channel, return its SKILL.md body
so the prompt-assembly layer can surface it inline.

The other half (the per-poller action hint right next to the
event itself) lives in the individual poller scripts (e.g.
``mimir/optional-skills/social-cli/poller.py``).

Behavior
========
- ``find_skill_for_channel(channel_id, skills_dirs)`` returns
  ``(skill_name, skill_md_body)`` or ``None``. The body has the YAML
  frontmatter stripped — only the operator-facing content.
- Only ``poller:<name>`` channels are matched. ``scheduler:<name>``
  channels are NOT mapped because most scheduler jobs are callables,
  not skills (saga-consolidate, identities-populate, viability-report,
  etc.). The reflection turn (channel ``scheduler:reflect``) is the
  exception — it could reasonably auto-surface the reflection skill
  — but it already has dedicated prompt-template scaffolding via
  ``mimir.prompt_templates/reflect.md``, so we don't double up.
- Non-poller channels return ``None``: the auto-surfacing is purely
  for the case where an event landed on a poller-driven channel.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterable

from .skill_md import strip_frontmatter as _strip_frontmatter  # canonical; dedup via chainlink #212

log = logging.getLogger(__name__)


_POLLER_CHANNEL_PREFIX = "poller:"


def _skill_dirs_for_poller(
    poller_name: str,
    skills_dirs: Iterable[Path],
) -> Path | None:
    """Return the skill directory whose ``pollers.json`` declares
    ``poller_name``, or ``None`` when no skill matches.

    Operator-installed (``<home>/skills/``) shadows bundled
    (``<home>/.mimir_builtin_skills/``) — when both directories
    contain a skill registering the same poller name, the operator
    copy wins. This mirrors the catalog-resolution rule used
    elsewhere (``installed_skill_names``).
    """
    for skills_dir in skills_dirs:
        if not skills_dir.is_dir():
            continue
        try:
            entries = sorted(skills_dir.iterdir())
        except OSError:
            continue
        for skill_dir in entries:
            if not skill_dir.is_dir():
                continue
            pollers_json = skill_dir / "pollers.json"
            if not pollers_json.is_file():
                continue
            try:
                raw = pollers_json.read_text(encoding="utf-8")
                data = json.loads(raw)
            except (OSError, json.JSONDecodeError) as exc:
                log.debug(
                    "could not parse %s for skill-lookup: %s",
                    pollers_json, exc,
                )
                continue
            if not isinstance(data, dict):
                continue
            pollers = data.get("pollers")
            if not isinstance(pollers, list):
                continue
            for p in pollers:
                if isinstance(p, dict) and p.get("name") == poller_name:
                    return skill_dir
    return None


def find_skill_for_channel(
    channel_id: str | None,
    skills_dirs: Iterable[Path],
) -> tuple[str, str] | None:
    """Return ``(skill_name, body)`` when ``channel_id`` is a
    ``poller:<name>`` channel whose poller is declared by an
    installed skill. ``body`` is the SKILL.md content with YAML
    frontmatter stripped — suitable for inlining as a prompt section.

    Returns ``None`` when:
    - ``channel_id`` is empty / None / not a poller channel
    - no installed skill declares the poller
    - the matched skill has no readable SKILL.md
    """
    if not channel_id or not channel_id.startswith(_POLLER_CHANNEL_PREFIX):
        return None
    poller_name = channel_id[len(_POLLER_CHANNEL_PREFIX):].strip()
    if not poller_name:
        return None
    skill_dir = _skill_dirs_for_poller(poller_name, skills_dirs)
    if skill_dir is None:
        return None
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return None
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError as exc:
        log.debug("could not read %s: %s", skill_md, exc)
        return None
    body = _strip_frontmatter(text)
    if not body.strip():
        return None
    return (skill_dir.name, body)
