"""Command-line entrypoint for mimir.

Subcommands:
- ``mimir setup [--home DIR]`` — scaffold an agent home (dirs, .env template,
  scheduler.yaml stub, skills, subagent defs). Idempotent — never overwrites
  existing files.
- ``mimir run [--home DIR]``   — run the server (default if no subcommand).
- ``mimir identities {list,add,remove,resolve}`` — manage identity
  reconciliation entries (FUTURE_WORK §6.1).

Both run/setup commands export ``MIMIR_HOME`` to the resolved path before
loading ``Config.from_env()``, so the CLI flag and the env var converge.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import secrets
import sys
from pathlib import Path
from textwrap import dedent
from typing import Sequence

import yaml

from .identities import IdentityResolver
from .skill_defs import seed_skills
from .subagent_defs import seed_subagent_defs


DEFAULT_ENV_TEMPLATE = dedent(
    """\
    # mimir environment — fill in what you use, leave the rest blank.

    # ---- LLM gateway (Anthropic-compatible) ------------------------------
    # For Claude direct: set ANTHROPIC_API_KEY.
    # For Minimax / Moonshot / other gateways: set ANTHROPIC_BASE_URL +
    # ANTHROPIC_AUTH_TOKEN (and ANTHROPIC_MODEL if the gateway needs it).
    ANTHROPIC_API_KEY=
    ANTHROPIC_BASE_URL=
    ANTHROPIC_AUTH_TOKEN=
    ANTHROPIC_MODEL=
    ANTHROPIC_CUSTOM_MODEL_OPTION=

    # ---- MSAM sidecar (memory) -------------------------------------------
    MSAM_ENDPOINT=http://localhost:3002
    MSAM_API_KEY=

    # ---- Channel bridges (all optional) ----------------------------------
    DISCORD_TOKEN=
    SLACK_BOT_TOKEN=
    SLACK_APP_TOKEN=
    BSKY_HANDLE=
    BSKY_APP_PASSWORD=

    # ---- Server tuning ---------------------------------------------------
    MIMIR_WEB_PORT=8080
    MIMIR_MODEL=claude-opus-4-7
    MIMIR_EFFORT=high
    # API key for the public injection endpoint (POST /event). When set,
    # requests must carry a matching ``X-API-Key`` header. The server
    # binds to 0.0.0.0 — any non-localhost deployment must have this set.
    # ``mimir setup`` auto-generates a value here on first run; rotate
    # with ``mimir regenerate-api-key``. Leave blank only for local-dev
    # mode where you understand the implications.
    MIMIR_API_KEY=

    # ---- Operator config -------------------------------------------------
    # Channel the agent uses for high-priority signals to you that don't fit
    # the current conversation (critical errors, urgent heartbeat findings,
    # dispatch failures). Leave blank to disable. Use a normal channel_id —
    # typically your DM with the bot, e.g. dm-slack-U05XXXX or dm-discord-NNN.
    MIMIR_OPERATOR_ALERT_CHANNEL=
    """
)


DEFAULT_SCHEDULER_YAML = dedent(
    """\
    # mimir scheduler — APScheduler cron jobs that enqueue LLM ticks.
    # Each job triggers a turn on ``channel_id`` with ``trigger=scheduled_tick``.
    #
    # Example one-off job:
    #
    # jobs:
    #   - name: morning-checkin
    #     cron: "0 9 * * 1-5"
    #     channel_id: web-default
    #     prompt: "Morning check-in: review yesterday and plan today."
    #
    # Heartbeat tick (v0.4 §1) — autonomous-work cadence. Uncomment to
    # enable. When ``prompt:`` is empty/omitted on a scheduled_tick, the
    # turn prompt falls back to HEARTBEAT_DEFAULT_PROMPT (see
    # mimir/prompts.py); having an explicit prompt here keeps the
    # configuration self-documenting.
    #
    # jobs:
    #   - name: heartbeat
    #     cron: "*/30 * * * *"
    #     channel_id: null   # synthetic scheduler:heartbeat channel
    #     prompt: "Run the heartbeat skill: librarian protocol first, then pick one item from state/heartbeat-backlog.md and do it. End silently."
    #
    # Reflection (v0.4 §4) — weekly cross-session audit. Sunday 04:00 UTC
    # so it lands after the MSAM weekly consolidation. Uncomment to
    # enable.
    #
    # jobs:
    #   - name: reflect
    #     cron: "0 4 * * 0"
    #     channel_id: null   # synthetic scheduler:reflect channel
    #     prompt: "Run the reflection skill — cross-session audit of the past week."

    jobs: []
    """
)


DEFAULT_HEARTBEAT_BACKLOG = dedent(
    """\
    # Heartbeat Backlog

    Tasks for autonomous work during scheduled heartbeats. Operator and
    agent both append.

    Format per item:

    ```
    - [ ] **<short name>** [YYYY-MM-DD added] — one-line what
      - What: ...
      - Why: ...
      - How: ...
      - Frequency: <daily | weekly | once>
      - Priority: <HIGH | MEDIUM | LOW>
      - Last completed: <YYYY-MM-DD or "never">
      - Skill: <relative path or skill name, optional>
    ```

    ## Active Backlog

    (Discrete tasks; pick one per heartbeat. Operator seeds initial
    items here; agent may append observations.)

    ## Standing Tasks

    (Daily / weekly / recurring; agent updates `Last completed:` after
    each run. Pick one whose slot for today is open.)
    """
)


DEFAULT_HEARTBEAT_PATTERNS = dedent(
    """\
    <!-- desc: what works (and what doesn't) during heartbeat ticks -->
    # Heartbeat Patterns

    Empty starter. Append observations from your heartbeat experience —
    tasks that fit well, ones that didn't, time-of-day patterns,
    mistakes worth not repeating. Keep it tight; this block is in core
    memory.
    """
)


DEFAULT_REFLECTION_POLICY = dedent(
    """\
    <!-- desc: which reflection actions are autonomous vs propose-only -->
    # Reflection Policy

    Read by the reflection skill at the start of every weekly audit.
    Edit this file to widen or tighten the autonomous boundary as
    trust builds. Conservative defaults:

    ## Autonomous (the reflection turn may apply directly)

    - MSAM atom decay calls
    - MSAM triples linking (additive)
    - Append-only edits to memory/core/40-learned-behaviors.md
    - Wiki orphan tagging (writes to state/wiki/index.md — flag, don't delete)

    ## Propose-only (write to state/proposed-changes.md, operator reviews)

    - Core memory edits (cleanup, restructure, promote-to-core, demote)
    - Persona block edits (memory/core/00-*.md)
    - Skill creation (.claude/skills/<name>/)
    - Wiki page deletions
    - Memory file deletions

    If this file is missing or unparseable, fall back to propose-only
    for everything — never auto-apply when in doubt.
    """
)


DEFAULT_LEARNED_BEHAVIORS = dedent(
    """\
    <!-- desc: behaviors learned through reflection - autonomous additions only -->
    # Learned Behaviors

    Append-only. The reflection turn writes here when it observes a
    pattern worth keeping (a recurring approach that worked, a
    failure mode worth avoiding, a heuristic that emerged across
    several sessions). Never edit prior entries from a reflection —
    propose any restructure via state/proposed-changes.md instead.

    Format per entry:

    ```
    ## YYYY-MM-DD — short title
    What I noticed: ...
    What works: ...
    Trigger: <when this applies>
    ```
    """
)


DEFAULT_PROPOSED_CHANGES = dedent(
    """\
    # Proposed Changes

    Pending HITL items from the reflection skill. Operator reviews on
    their own cadence; once an item is applied or rejected, move it
    to a `## Applied` / `## Rejected` section below or remove it.

    Format per item:

    ```
    ## YYYY-MM-DD — short title
    Source: <reflection week / heartbeat tick / other>
    Proposal: <what to change>
    Rationale: <why>
    Affected: <file paths or systems>
    ```

    ## Pending

    (empty — populated by reflection)

    ## Applied

    (operator moves accepted items here, optionally with notes)

    ## Rejected

    (operator moves rejected items here, optionally with notes)
    """
)


DEFAULT_IDENTITY_MD = dedent(
    """\
    # Identity

    You are mimir — a memory-centric agent. Update this file with the
    persona, voice, and goals you want to keep across every conversation.
    This is loaded into ``memory/core/`` and read on every turn.
    """
)


DEFAULT_WIKI_AGENTS_MD = dedent(
    """\
    # AGENTS.md

    Schema for maintaining the wiki under ``state/wiki/``. The full skill
    is at ``.claude/skills/wiki/SKILL.md`` — this file is a quick reference.

    ## Three layers

    1. **Raw sources** — ``state/raw/`` — immutable source documents,
       never modified after landing.
    2. **Wiki** — ``state/wiki/`` — your synthesis with cross-references.
    3. **Schema** — this file — conventions for maintaining the wiki.

    ## Categories

    - ``entities/`` — named things (people, agents, organizations, products)
    - ``concepts/`` — abstract ideas, patterns, frameworks
    - ``topics/`` — concrete subjects, projects, events

    ## Conventions

    - Frontmatter: ``title``, ``description``, ``type``, optional ``tags``.
      Descriptive, not enforced — typos won't break anything.
    - Wikilinks: ``[[page-name]]``. Add inline in prose AND in a Related
      section. Links are not optional — they make the wiki a graph.
    - Each page should have a "Connection to My Work" section so it's
      synthesis, not summary.

    ## Operations (see SKILL.md for detail)

    - **Ingest:** raw/ → wiki/. Read source, create/update page, link.
    - **Query:** search wiki/ first; only fall back to raw/ if needed.
    - **Lint:** periodic — orphan pages, missing cross-refs, stale claims.
    """
)


DEFAULT_WIKI_INDEX_MD = dedent(
    """\
    # Wiki Index

    Catalog of wiki pages. Update on every ingest.

    ## Entities

    (none yet)

    ## Concepts

    (none yet)

    ## Topics

    (none yet)
    """
)


DEFAULT_WIKI_LOG_MD = dedent(
    """\
    # Wiki Log

    Chronological record of wiki operations. Append on every ingest / lint.

    Format:
    ```
    YYYY-MM-DD — <operation>: <file(s) affected>
    ```
    """
)


DEFAULT_IDENTITIES_YAML = dedent(
    """\
    # Operator-managed identity reconciliation (FUTURE_WORK §6.1).
    #
    # Each person has a canonical id and a list of platform aliases.
    # When messages arrive with these aliases as authors, the resolver
    # maps them to the canonical so cross-channel pull works across
    # platforms (Alice on Slack pulls her Discord public history, etc.).
    #
    # Add entries as you learn cross-platform identities. The agent
    # doesn't write this file — only operators and the (future)
    # `mimir identities` CLI do.
    #
    # Schema:
    #
    # people:
    #   - canonical: alice                    # short id used as the matching key
    #     display_name: Alice Smith           # optional; for prompt rendering
    #     aliases:
    #       - slack-U123ABC                   # Slack user id
    #       - discord-456789                  # Discord numeric id
    #       - bsky:alice.bsky.social          # Bluesky handle
    #       - email:alice@example.com         # email address
    #     notes: Eng team lead                # optional; surfaces in prompt
    #
    # Alias prefix conventions (informational — resolver treats aliases
    # as opaque strings, so the prefix is for readability only):
    #   slack-<id>      hyphen, alphanumeric id
    #   discord-<id>    hyphen, numeric id
    #   bsky:<handle>   colon (handle contains dots)
    #   email:<addr>    colon (address contains @ and dots)
    #
    # Operators can disable cross-platform pull entirely (compliance,
    # regulated workflows) by setting MIMIR_CROSS_PLATFORM_PULL=false
    # in .env. The resolver still loads but cross_author_messages
    # falls back to direct equality.

    people: []
    """
)


def _write_if_missing(path: Path, content: str) -> bool:
    """Write ``content`` to ``path`` only if the file doesn't exist.

    Returns True if the file was created.
    """
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def _generate_api_key() -> str:
    """A 256-bit URL-safe random token. Roughly 43 chars; safe for shells
    and Docker env files (no quoting, no escaping)."""
    return secrets.token_urlsafe(32)


# Match `MIMIR_API_KEY=<anything>` (line-anchored, with optional leading
# whitespace). The replacement preserves the leading whitespace so an
# operator's indentation in their .env stays intact.
_API_KEY_LINE_RE = re.compile(r"^(\s*)MIMIR_API_KEY\s*=.*$", re.MULTILINE)


def _env_set_api_key(env_path: Path, value: str) -> None:
    """Rewrite the MIMIR_API_KEY line in ``env_path`` with ``value``.
    Appends a new line if the var isn't present. Leaves all other lines
    untouched."""
    body = env_path.read_text(encoding="utf-8") if env_path.is_file() else ""
    new_line = f"MIMIR_API_KEY={value}"
    if _API_KEY_LINE_RE.search(body):
        body = _API_KEY_LINE_RE.sub(
            lambda m: f"{m.group(1)}{new_line}", body, count=1
        )
    else:
        if body and not body.endswith("\n"):
            body += "\n"
        body += new_line + "\n"
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(body, encoding="utf-8")


def _env_get_api_key(env_path: Path) -> str | None:
    """Return the current MIMIR_API_KEY value (possibly empty), or None
    if the var isn't set in the file. Empty string means "set but blank";
    None means "not present at all"."""
    if not env_path.is_file():
        return None
    body = env_path.read_text(encoding="utf-8")
    match = _API_KEY_LINE_RE.search(body)
    if not match:
        return None
    line = match.group(0)
    _, _, value = line.partition("=")
    return value.strip()


def setup_home(home: Path) -> dict[str, object]:
    """Scaffold an agent home directory. Returns a status dict for printing."""
    home = home.resolve()
    if home.exists() and not home.is_dir():
        raise ValueError(
            f"--home {home} exists and is not a directory; refusing to scaffold over it."
        )
    home.mkdir(parents=True, exist_ok=True)

    created_dirs: list[str] = []
    for sub in (
        "logs",
        "memory/core",
        "memory/channels",
        "memory/shared",
        "state",
        "state/raw",
        "state/wiki",
        "state/wiki/entities",
        "state/wiki/concepts",
        "state/wiki/topics",
        "messages",
        ".claude/agents",
        ".claude/skills",
    ):
        p = home / sub
        if not p.exists():
            created_dirs.append(sub)
        p.mkdir(parents=True, exist_ok=True)

    files_created: list[str] = []
    api_key_action: str | None = None
    if _write_if_missing(home / ".env", DEFAULT_ENV_TEMPLATE):
        files_created.append(".env")
    # Generate a fresh MIMIR_API_KEY on first setup (or if the operator
    # left the value blank). Existing non-empty keys are preserved on
    # re-run — operators can rotate via `mimir regenerate-api-key`.
    if (_env_get_api_key(home / ".env") or "") == "":
        _env_set_api_key(home / ".env", _generate_api_key())
        api_key_action = "generated"
    if _write_if_missing(home / "scheduler.yaml", DEFAULT_SCHEDULER_YAML):
        files_created.append("scheduler.yaml")
    if _write_if_missing(home / "memory" / "core" / "identity.md", DEFAULT_IDENTITY_MD):
        files_created.append("memory/core/identity.md")
    if _write_if_missing(home / "state" / "wiki" / "AGENTS.md", DEFAULT_WIKI_AGENTS_MD):
        files_created.append("state/wiki/AGENTS.md")
    if _write_if_missing(home / "state" / "wiki" / "index.md", DEFAULT_WIKI_INDEX_MD):
        files_created.append("state/wiki/index.md")
    if _write_if_missing(home / "state" / "wiki" / "log.md", DEFAULT_WIKI_LOG_MD):
        files_created.append("state/wiki/log.md")
    if _write_if_missing(home / "state" / "identities.yaml", DEFAULT_IDENTITIES_YAML):
        files_created.append("state/identities.yaml")
    if _write_if_missing(
        home / "state" / "heartbeat-backlog.md", DEFAULT_HEARTBEAT_BACKLOG
    ):
        files_created.append("state/heartbeat-backlog.md")
    if _write_if_missing(
        home / "memory" / "core" / "50-heartbeat-patterns.md",
        DEFAULT_HEARTBEAT_PATTERNS,
    ):
        files_created.append("memory/core/50-heartbeat-patterns.md")
    if _write_if_missing(
        home / "memory" / "core" / "30-reflection-policy.md",
        DEFAULT_REFLECTION_POLICY,
    ):
        files_created.append("memory/core/30-reflection-policy.md")
    if _write_if_missing(
        home / "memory" / "core" / "40-learned-behaviors.md",
        DEFAULT_LEARNED_BEHAVIORS,
    ):
        files_created.append("memory/core/40-learned-behaviors.md")
    if _write_if_missing(
        home / "state" / "proposed-changes.md", DEFAULT_PROPOSED_CHANGES
    ):
        files_created.append("state/proposed-changes.md")

    seeded_subagents = seed_subagent_defs(home)
    seeded_skills = seed_skills(home)

    return {
        "home": str(home),
        "dirs_created": created_dirs,
        "files_created": files_created,
        "subagents": seeded_subagents,
        "skills": seeded_skills,
        "api_key_action": api_key_action,
    }


def _print_setup_report(status: dict[str, object]) -> None:
    home = status["home"]
    print(f"mimir home ready at: {home}")
    if status["dirs_created"]:
        print(f"  created dirs:  {', '.join(status['dirs_created'])}")  # type: ignore[arg-type]
    if status["files_created"]:
        print(f"  wrote files:   {', '.join(status['files_created'])}")  # type: ignore[arg-type]
    skills = status["skills"]
    subs = status["subagents"]
    if isinstance(skills, dict):
        new_skills = sorted(n for n, s in skills.items() if s == "created")
        if new_skills:
            print(f"  skills seeded: {', '.join(new_skills)}")
    if isinstance(subs, dict):
        new_subs = sorted(n for n, s in subs.items() if s == "created")
        if new_subs:
            print(f"  subagents seeded: {', '.join(new_subs)}")
    if status.get("api_key_action") == "generated":
        print("  MIMIR_API_KEY:  generated (see .env; rotate via `mimir regenerate-api-key`)")
    print()
    print("Next steps:")
    print(f"  1. Edit {home}/.env (LLM gateway + any bridge tokens)")
    print(f"  2. (optional) Edit {home}/memory/core/identity.md")
    print(f"  3. Run:  mimir run --home {home}")


# ---------------------------------------------------------------------------
# `mimir identities` subcommand (FUTURE_WORK §6.1)
# ---------------------------------------------------------------------------


def _identities_load(yaml_path: Path) -> dict:
    """Load state/identities.yaml as a mutable dict. Missing file or empty
    body returns ``{"people": []}``. Raises ``ValueError`` on parse error
    (so the CLI fails loudly rather than overwriting an unreadable file)."""
    if not yaml_path.is_file():
        return {"people": []}
    text = yaml_path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"identities.yaml parse failed: {exc}") from exc
    if not isinstance(data, dict):
        return {"people": []}
    if not isinstance(data.get("people"), list):
        data["people"] = []
    return data


def _identities_save(yaml_path: Path, data: dict) -> None:
    """Atomic write via ``<file>.tmp + rename``. Same pattern as scheduler.yaml.

    Note: this loses the comment header from the starter template. Once
    the operator runs the CLI, the file becomes machine-managed; the
    schema documentation lives in ``mimir/identities.py`` and
    FUTURE_WORK §6.1 instead.
    """
    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = yaml_path.with_suffix(".yaml.tmp")
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    tmp.write_text(body, encoding="utf-8")
    tmp.rename(yaml_path)


def _identities_list_cmd(yaml_path: Path) -> None:
    data = _identities_load(yaml_path)
    people = data.get("people") or []
    if not people:
        print("(no identities defined)")
        return
    for entry in people:
        canonical = entry.get("canonical", "?")
        display = entry.get("display_name") or ""
        notes = entry.get("notes") or ""
        aliases = entry.get("aliases") or []
        head = f"- {canonical}"
        if display:
            head += f" — {display}"
        if notes:
            head += f" ({notes})"
        print(head)
        for alias in aliases:
            print(f"    {alias}")


def _identities_add_cmd(
    yaml_path: Path,
    canonical: str,
    alias: str,
    display_name: str | None,
    notes: str | None,
) -> None:
    data = _identities_load(yaml_path)
    people: list = data.setdefault("people", [])

    # Reject if alias is already claimed by a different canonical — collisions
    # in the alias map are last-wins at load, but the operator probably wants
    # the CLI to surface the conflict instead of silently overwriting.
    for entry in people:
        for existing_alias in entry.get("aliases") or []:
            if existing_alias == alias and entry.get("canonical") != canonical:
                raise ValueError(
                    f"alias {alias!r} already maps to canonical "
                    f"{entry.get('canonical')!r}; remove it first or use a "
                    f"different alias"
                )

    target = next((e for e in people if e.get("canonical") == canonical), None)
    if target is None:
        target = {"canonical": canonical, "aliases": []}
        people.append(target)

    if display_name:
        target["display_name"] = display_name
    if notes:
        target["notes"] = notes
    aliases = target.setdefault("aliases", [])
    if alias not in aliases:
        aliases.append(alias)

    _identities_save(yaml_path, data)
    print(f"added: {canonical} ← {alias}")


def _identities_remove_cmd(
    yaml_path: Path,
    alias: str | None,
    canonical: str | None,
) -> None:
    data = _identities_load(yaml_path)
    people: list = data.get("people") or []

    if canonical:
        before = len(people)
        people[:] = [p for p in people if p.get("canonical") != canonical]
        if len(people) == before:
            print(f"(no identity with canonical {canonical!r})")
            return
        data["people"] = people
        _identities_save(yaml_path, data)
        print(f"removed identity: {canonical}")
        return

    if alias:
        for entry in people:
            aliases = entry.get("aliases") or []
            if alias in aliases:
                aliases.remove(alias)
                # Drop the entire identity when its last alias is gone —
                # otherwise the entry sits in state/identities.yaml as a
                # canonical-only stub that the resolver loads as a
                # no-op and that future `add` calls treat as a real
                # pre-existing identity.
                if not aliases:
                    canonical = entry.get("canonical")
                    people[:] = [p for p in people if p is not entry]
                    _identities_save(yaml_path, data)
                    print(
                        f"removed alias: {alias} (and {canonical}: "
                        "no aliases remained)"
                    )
                    return
                _identities_save(yaml_path, data)
                print(f"removed alias: {alias} (from {entry.get('canonical')})")
                return
        print(f"(alias {alias!r} not found)")


def regenerate_api_key(home: Path) -> str:
    """Rewrite ``<home>/.env``'s MIMIR_API_KEY line with a fresh random
    value. Returns the new key. Other env vars are left untouched."""
    home = home.resolve()
    env_path = home / ".env"
    new_key = _generate_api_key()
    _env_set_api_key(env_path, new_key)
    return new_key


def _identities_resolve_cmd(home: Path, author: str) -> None:
    resolver = IdentityResolver(home=home)
    resolver.reload()
    canonical = resolver.resolve(author)
    if canonical == author:
        print(f"{author} → (no identity record; falls through to itself)")
        return
    display = resolver.display_name(author)
    suffix = f" ({display})" if display else ""
    print(f"{author} → {canonical}{suffix}")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="mimir",
        description="Memory-centric agent harness on the Claude Agent SDK.",
    )
    sub = parser.add_subparsers(dest="command")

    setup_p = sub.add_parser(
        "setup",
        help="Scaffold a mimir home (dirs, .env, scheduler.yaml, skills, subagents).",
    )
    setup_p.add_argument(
        "--home", type=Path, default=Path.cwd(),
        help="Target directory (default: current working dir).",
    )

    run_p = sub.add_parser("run", help="Run the mimir server (default).")
    run_p.add_argument(
        "--home", type=Path, default=None,
        help="Agent home (overrides MIMIR_HOME; default: cwd).",
    )

    # `mimir identities {list,add,remove,resolve}` — manage the alias map
    # at <home>/state/identities.yaml. Operator-facing; the agent doesn't
    # use this CLI (FUTURE_WORK §6.1).
    id_p = sub.add_parser(
        "identities",
        help="Manage identity reconciliation entries (state/identities.yaml).",
    )
    id_sub = id_p.add_subparsers(dest="identities_action")

    id_list_p = id_sub.add_parser("list", help="Show all identities.")
    id_list_p.add_argument("--home", type=Path, default=Path.cwd())

    id_add_p = id_sub.add_parser(
        "add",
        help="Add (or extend) an identity with an alias.",
    )
    id_add_p.add_argument("--home", type=Path, default=Path.cwd())
    id_add_p.add_argument("--canonical", required=True, help="Canonical id (e.g. 'alice').")
    id_add_p.add_argument(
        "--alias",
        required=True,
        help="Platform-prefixed alias (e.g. 'slack-U05ALICE', 'discord-456789', "
             "'bsky:alice.bsky.social', 'email:alice@example.com').",
    )
    id_add_p.add_argument("--display-name", default=None, help="Optional display name.")
    id_add_p.add_argument("--notes", default=None, help="Optional notes (surfaces in prompt).")

    id_rm_p = id_sub.add_parser(
        "remove",
        help="Remove an alias or an entire identity.",
    )
    id_rm_p.add_argument("--home", type=Path, default=Path.cwd())
    rm_group = id_rm_p.add_mutually_exclusive_group(required=True)
    rm_group.add_argument("--alias", help="Alias to remove (from whichever identity owns it).")
    rm_group.add_argument("--canonical", help="Canonical id of an identity to remove entirely.")

    id_resolve_p = id_sub.add_parser(
        "resolve",
        help="Diagnostic: show what an author id maps to.",
    )
    id_resolve_p.add_argument("--home", type=Path, default=Path.cwd())
    id_resolve_p.add_argument("author", help="Author id to resolve (e.g. 'slack-U05ALICE').")

    # `mimir reflection <action>` — bundled-script subcommands the
    # reflection skill invokes from agent Bash. Pattern: each bundled
    # script that needs CLI access registers a subcommand under its
    # parent skill's verb. Avoids the cwd/PATH brittleness of
    # `python -m mimir.skills.reflection.…`; ``mimir`` is on PATH
    # wherever the operator launched the server from.
    regen_p = sub.add_parser(
        "regenerate-api-key",
        help="Rotate MIMIR_API_KEY in <home>/.env. Prints the new value.",
    )
    regen_p.add_argument(
        "--home",
        type=Path,
        default=None,
        help="Agent home (overrides MIMIR_HOME; default: cwd).",
    )

    refl_p = sub.add_parser(
        "reflection",
        help="Reflection skill helpers (invoked by skills/reflection/SKILL.md).",
    )
    refl_sub = refl_p.add_subparsers(dest="reflection_action")

    refl_mr_p = refl_sub.add_parser(
        "most-retrieved",
        help="Top-N MSAM atoms by retrieval count over the last N days.",
    )
    from .skills.reflection import most_retrieved as _most_retrieved
    _most_retrieved.add_argparse(refl_mr_p)

    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "setup":
        status = setup_home(args.home)
        _print_setup_report(status)
        return

    if args.command == "identities":
        if args.identities_action is None:
            id_p.print_help()
            sys.exit(1)
        home = Path(args.home).resolve()
        yaml_path = home / "state" / "identities.yaml"
        try:
            if args.identities_action == "list":
                _identities_list_cmd(yaml_path)
            elif args.identities_action == "add":
                _identities_add_cmd(
                    yaml_path,
                    canonical=args.canonical,
                    alias=args.alias,
                    display_name=args.display_name,
                    notes=args.notes,
                )
            elif args.identities_action == "remove":
                _identities_remove_cmd(
                    yaml_path,
                    alias=args.alias,
                    canonical=args.canonical,
                )
            elif args.identities_action == "resolve":
                _identities_resolve_cmd(home, args.author)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(1)
        return

    if args.command == "regenerate-api-key":
        home_arg = args.home or os.environ.get("MIMIR_HOME") or Path.cwd()
        home = Path(home_arg).resolve()
        env_path = home / ".env"
        if not env_path.is_file():
            print(
                f"error: no .env at {env_path}; run `mimir setup` first",
                file=sys.stderr,
            )
            sys.exit(1)
        new_key = regenerate_api_key(home)
        print(new_key)
        print(
            f"\nWrote to {env_path}. Restart `mimir run` for the new key to take effect.",
            file=sys.stderr,
        )
        return

    if args.command == "reflection":
        if args.reflection_action == "most-retrieved":
            from .skills.reflection import most_retrieved as _most_retrieved
            sys.exit(asyncio.run(_most_retrieved.run(args)))
        refl_p.print_help()
        sys.exit(1)

    if args.command in (None, "run"):
        home_arg = getattr(args, "home", None)
        if home_arg is not None:
            os.environ["MIMIR_HOME"] = str(Path(home_arg).resolve())
        # Defer import — server pulls in aiohttp/SDK; keep `mimir setup`
        # snappy and importable in environments where the runtime isn't
        # fully wired up yet.
        from .server import main as run_server

        run_server()
        return

    parser.print_help()
    sys.exit(1)


if __name__ == "__main__":
    main()
