"""Command-line entrypoint for mimir.

Subcommands:
- ``mimir setup [--home DIR]`` — scaffold an agent home (dirs, .env template,
  scheduler.yaml stub, skills, subagent defs). Idempotent — never overwrites
  existing files.
- ``mimir run [--home DIR]``   — run the server (default if no subcommand).

Both commands export ``MIMIR_HOME`` to the resolved path before loading
``Config.from_env()``, so the CLI flag and the env var converge.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from textwrap import dedent
from typing import Sequence

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
    """
)


DEFAULT_SCHEDULER_YAML = dedent(
    """\
    # mimir scheduler — APScheduler cron jobs that enqueue LLM ticks.
    # Each job triggers a turn on ``channel_id`` with ``trigger=cron_tick``.
    #
    # jobs:
    #   - id: morning-checkin
    #     cron: "0 9 * * 1-5"
    #     channel_id: web-default
    #     content: "Morning check-in: review yesterday and plan today."

    jobs: []
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
    if _write_if_missing(home / ".env", DEFAULT_ENV_TEMPLATE):
        files_created.append(".env")
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

    seeded_subagents = seed_subagent_defs(home)
    seeded_skills = seed_skills(home)

    return {
        "home": str(home),
        "dirs_created": created_dirs,
        "files_created": files_created,
        "subagents": seeded_subagents,
        "skills": seeded_skills,
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
    print()
    print("Next steps:")
    print(f"  1. Edit {home}/.env (LLM gateway + any bridge tokens)")
    print(f"  2. (optional) Edit {home}/memory/core/identity.md")
    print(f"  3. Run:  mimir run --home {home}")


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

    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "setup":
        status = setup_home(args.home)
        _print_setup_report(status)
        return

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
