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
import logging
import os
import sys
from pathlib import Path
from typing import Sequence

import yaml

from .identities import IdentityResolver

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Re-exports from commands.setup (backward compatibility — tests and external
# code that imports from mimir.cli continue to work unchanged).
# ---------------------------------------------------------------------------
from .commands.setup import (  # noqa: E402
    setup_home,
    _print_setup_report,
    regenerate_api_key,
    _skill_env_summary,
    EMBEDDING_PRESETS,
    DEFAULT_EMBEDDING_PRESET,
    DEFAULT_ENV_TEMPLATE,
    DEFAULT_HEARTBEAT_BACKLOG,
    DEFAULT_HEARTBEAT_PATTERNS,
    DEFAULT_VSM_TERMS,
    DEFAULT_REFLECTION_POLICY,
    DEFAULT_LEARNED_BEHAVIORS,
    DEFAULT_FILING_RULES,
    DEFAULT_ISSUES_README,
    DEFAULT_PROPOSED_CHANGES,
    DEFAULT_IDENTITY_MD,
    DEFAULT_ACTION_BOUNDARIES,
    DEFAULT_WIKI_AGENTS_MD,
    DEFAULT_WIKI_INDEX_MD,
    DEFAULT_WIKI_LOG_MD,
    DEFAULT_IDENTITIES_YAML,
)


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


def _cmd_update(args) -> int:
    """``mimir update`` — PyPI version-check + optional install.

    Three modes:
    - default: print current vs latest, exit 0 either way
    - ``--check``: same status print, but exit 1 if an update is
      available (useful in scripts / CI gates)
    - ``--apply``: if newer version available, run
      ``python -m pip install --upgrade mimir``. After install,
      print a clear "restart agent" message — the running process
      doesn't auto-pick up the new install.

    ``--pre`` flag opts into pre-release surfacing (alpha / beta / rc
    / dev). Default excludes them — stable releases only.
    """
    from .version_check import check_for_update, _pypi_package_name

    pkg = _pypi_package_name()
    result = check_for_update(include_prereleases=args.pre)

    if result.error_msg:
        print(f"Update check failed: {result.error_msg}", file=sys.stderr)
        print(f"Current installed: {result.current}", file=sys.stderr)
        # Exit 0 — failure isn't actionable; treat as "no signal".
        # --check should NOT report "update available" on a failure.
        return 0

    print(f"Current installed: {result.current}")
    print(f"Latest on PyPI:    {result.latest}")
    if not result.is_newer:
        print("Status: up to date")
        return 0

    print("Status: UPDATE AVAILABLE")
    if not args.apply:
        if args.check:
            # CI / script flow — non-zero so callers can act.
            return 1
        print()
        print("To install:  mimir update --apply")
        print("After install, restart the agent:")
        print("  docker compose restart   # containerized deployments")
        print("  (or restart the mimir process by hand for bare-metal)")
        return 0

    # --apply path. Use python -m pip directly so this works regardless
    # of whether the operator's environment uses pip, uv, or pipx for
    # the original install — they all expose ``python -m pip``. The
    # package name is whatever ``MIMIR_PYPI_PACKAGE_NAME`` resolves to
    # (defaults to ``mimir`` for now; will be the real published name
    # once the open-source release is on PyPI).
    print()
    print(f"Installing {pkg} {result.latest} via python -m pip...")
    import subprocess
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", pkg],
            check=False,
        )
    except OSError as exc:
        print(f"pip invocation failed: {exc}", file=sys.stderr)
        return 2
    if completed.returncode != 0:
        print(
            f"pip exited {completed.returncode}; install did not succeed.",
            file=sys.stderr,
        )
        return completed.returncode
    print()
    print(f"Installed {pkg} {result.latest}.")
    print()
    print("IMPORTANT: the running agent process is still on the OLD")
    print("version until you restart it. To engage the new code:")
    print("  docker compose restart   # containerized deployments")
    print("  (or restart the mimir process by hand for bare-metal)")
    return 0


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
    setup_p.add_argument(
        "--embedding", type=str, default=DEFAULT_EMBEDDING_PRESET,
        choices=list(EMBEDDING_PRESETS),
        help=(
            f"Embedding provider preset for the generated saga.toml "
            f"(default: {DEFAULT_EMBEDDING_PRESET}). Voyage requires "
            f"VOYAGE_API_KEY; openai requires OPENAI_API_KEY; "
            f"nvidia-nim requires NVIDIA_NIM_API_KEY; fastembed is "
            f"fully local. saga's [consolidation] similarity_threshold "
            f"automatically tunes to the matching value (0.92 for "
            f"voyage/fastembed, 0.80 for openai/nvidia-nim)."
        ),
    )
    setup_p.add_argument(
        "--model", type=str, default=None,
        help=(
            "Bare model name (no provider prefix needed). Setup "
            "auto-routes based on the name: ``MiniMax-M2.7`` → Minimax "
            "(via Anthropic-compat endpoint); ``kimi-k2-*`` → "
            "Moonshot; ``gpt-*`` / ``o[1-4]-*`` → OpenAI; ``claude-*`` "
            "→ direct Anthropic API. Generates the right "
            "``MIMIR_MODEL_SPEC`` + ``ANTHROPIC_BASE_URL`` entries in "
            ".env. Also wires the usage monitor that matches the "
            "provider's billing model — subscription routes get quota "
            "polling; API routes get per-turn cost tracking with a "
            "default $/hr ceiling. Default model: claude-sonnet-4-6 "
            "via direct API. See ``mimir/model_registry.py`` for the "
            "full mapping."
        ),
    )
    setup_p.add_argument(
        "--subscription", action="store_true",
        help=(
            "Declare this deployment runs on a subscription plan for "
            "the chosen provider (not pay-per-token API billing). "
            "Effect is provider-polymorphic: Claude family swaps to "
            "``claude-code:`` (Max OAuth via subprocess — the "
            "protocol IS different); OpenAI / Minimax / Moonshot keep "
            "the same model_spec (same HTTP endpoint, just a "
            "different API token tier). Either way the usage monitor "
            "flips from cost-tracking to quota-polling. Without this "
            "flag, every route defaults to pay-per-token + cost "
            "monitoring."
        ),
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

    # `mimir stats` — operator-facing usage report. Same data the
    # turn prompt's "## Resource usage" section shows, dumped to
    # stdout for one-off inspection. Reads turns.jsonl tail-first;
    # cheap regardless of file size.
    stats_p = sub.add_parser(
        "stats",
        help="Show usage stats (cost, tokens, cache hit rate) over recent windows.",
    )
    stats_p.add_argument(
        "--home",
        type=Path,
        default=None,
        help="Agent home (overrides MIMIR_HOME; default: cwd).",
    )

    # `mimir verify-cred <name>` / `mimir verify-creds` — credential
    # verification probes (SPEC §16 item 14, Phase 2). Probes live in
    # mimir/cred_verify.py; this CLI is the thin shell. Returns exit 0
    # if live, 1 if stale, 2 if the credential name isn't registered.
    verify_cred_p = sub.add_parser(
        "verify-cred",
        help="Verify a single credential by name (e.g. GITHUB_TOKEN).",
    )
    verify_cred_p.add_argument(
        "name", help="Credential name from the registry (see ``mimir verify-creds``).",
    )
    verify_creds_p = sub.add_parser(
        "verify-creds",
        help="Verify all registered credentials (optionally filtered by type).",
    )
    verify_creds_p.add_argument(
        "--type", choices=["A", "B", "C", "D"], default=None,
        dest="cred_type",
        help="Only run probes for this consumer type (see docs/credentials.md).",
    )

    # ``mimir viability-report`` — collapse + curation metrics
    # (SPEC §16 items follow-up from VSM eval). Computes the three
    # collapse indicators (cosine sim, atom-citation Gini, topic
    # diversity) and write-side curation rate. Operator-facing
    # ad-hoc inspection; scheduler runs it weekly automatically.
    viability_p = sub.add_parser(
        "viability-report",
        help="Generate the collapse-detection + curation-rate viability report.",
    )
    viability_p.add_argument(
        "--home", type=Path, default=None,
        help="Agent home (overrides MIMIR_HOME; default: cwd).",
    )
    viability_p.add_argument(
        "--collapse-window-days", type=int, default=7,
        help="Trailing window for collapse indicators (default: 7).",
    )
    viability_p.add_argument(
        "--curation-window-days", type=int, default=28,
        help="Trailing window for curation rates (default: 28).",
    )
    viability_p.add_argument(
        "--no-write", action="store_true",
        help="Print only; don't write to state/reports/viability-YYYY-MM-DD.md.",
    )

    # ``mimir verify-index`` — index integrity probes (SPEC §8.3,
    # §16 item 16). Run all checks against the file-corpus and SAGA
    # databases; exit 0 if clean, 1 if any check fails. Scheduled
    # daily by the framework (cron 30 4 * * *, after saga-consolidate
    # at 04:00) — this CLI is for ad-hoc operator inspection.
    verify_index_p = sub.add_parser(
        "verify-index",
        help="Check SQLite + FTS5 + embedding-dim integrity of the file-corpus and SAGA indexes.",
    )
    verify_index_p.add_argument(
        "--db", choices=["index", "saga"], default=None,
        help="Run only one database's checks (default: both).",
    )
    verify_index_p.add_argument(
        "--home", type=Path, default=None,
        help="Agent home (overrides MIMIR_HOME; default: cwd).",
    )

    # ``mimir rotate`` — credential rotation (SPEC §16 item 14, Phase 3).
    # Atomic compose.env edit + force-recreate + post-rotation verify,
    # with rollback if verification fails. Run from the deployment dir.
    rotate_p = sub.add_parser(
        "rotate",
        help="Rotate a single env credential — compose.env edit + recreate + verify.",
    )
    rotate_p.add_argument(
        "--env", required=True, dest="env_name",
        help="Env var name to rotate (e.g. GITHUB_TOKEN).",
    )
    rotate_p.add_argument(
        "--from-file", default=None, dest="from_file",
        help="Read new value from this file. Default: read one line from stdin (getpass'd if a TTY).",
    )
    rotate_p.add_argument(
        "--service", default=None,
        help="Compose service name to recreate + verify against. Auto-detected if only one service.",
    )
    rotate_p.add_argument(
        "--deployment-dir", default=None, dest="deployment_dir",
        help="Directory containing compose.env + compose.yml. Default: cwd.",
    )
    rotate_p.add_argument(
        "--no-recreate", action="store_true", dest="skip_recreate",
        help="Skip docker compose recreate + verify; only update compose.env.",
    )

    loops_p = sub.add_parser(
        "loops",
        help="Show feedback-loop inventory + last-fire status (FUTURE_WORK §12.6b).",
    )
    loops_p.add_argument(
        "--home",
        type=Path,
        default=None,
        help="Agent home (overrides MIMIR_HOME; default: cwd).",
    )

    # ``mimir update`` — PyPI version-check + optional apply.
    update_p = sub.add_parser(
        "update",
        help="Check PyPI for a newer mimir release; with --apply, install it via pip.",
    )
    update_p.add_argument(
        "--check",
        action="store_true",
        help="Print status only; exit non-zero when an update is available "
             "(useful in scripts / CI).",
    )
    update_p.add_argument(
        "--apply",
        action="store_true",
        help="If a newer version is on PyPI, run `python -m pip install --upgrade mimir`. "
             "You must restart the agent (e.g., `docker compose restart`) to engage "
             "the new code.",
    )
    update_p.add_argument(
        "--pre",
        action="store_true",
        help="Include pre-release versions (alpha / beta / rc / dev) when checking.",
    )

    refl_p = sub.add_parser(
        "reflection",
        help="Reflection skill helpers (invoked by skills/reflection/SKILL.md).",
    )
    refl_sub = refl_p.add_subparsers(dest="reflection_action")

    refl_mr_p = refl_sub.add_parser(
        "most-retrieved",
        help="Top-N SAGA atoms by retrieval count over the last N days.",
    )
    from .reflection import most_retrieved as _most_retrieved
    _most_retrieved.add_argparse(refl_mr_p)

    # §12.2: applied-proposals audit — closes the double-loop.
    refl_ma_p = refl_sub.add_parser(
        "mark-applied",
        help="Move a proposal from '## Pending' to '## Applied' in "
             "state/proposed-changes.md and append to applied-proposals.jsonl.",
    )
    refl_ma_p.add_argument(
        "id_match",
        help="Substring of the proposal heading (case-insensitive).",
    )
    refl_ma_p.add_argument(
        "--home", type=Path, default=None,
        help="Agent home (overrides MIMIR_HOME; default: cwd).",
    )

    refl_intro_p = refl_sub.add_parser(
        "introspection-report",
        help="Weekly behavioral / health report from turns.jsonl + events.jsonl.",
    )
    from .reflection import introspection_report as _intro_report
    _intro_report.add_argparse(refl_intro_p)

    pred_p = sub.add_parser(
        "predictions",
        help="Predictions tracking CLI (skills/predictions/script.py).",
    )
    from .skills.predictions import script as _predictions_script
    _predictions_script.add_argparse(pred_p)

    # `mimir wiki <action>` — wiki maintenance CLI. The agent invokes
    # these from lint passes via Bash; operators run them ad hoc.
    # First (only) action: ``backlinks``. Future: ``lint`` could
    # combine multiple checks; ``promote`` could move pages between
    # categories. Same parent group lets all of them share the home
    # resolution / event-logger init pattern.
    wiki_p = sub.add_parser(
        "wiki",
        help="Wiki maintenance helpers (backlinks, future lint passes).",
    )
    wiki_sub = wiki_p.add_subparsers(dest="wiki_action")

    wiki_bl_p = wiki_sub.add_parser(
        "backlinks",
        help="Walk state/wiki/, write orphans.md / dangling-links.md / "
             "backlinks-index.md. Emits wiki_backlinks_unhealthy event "
             "when the wiki has orphans or dangling links.",
    )
    from . import wiki_backlinks as _wiki_backlinks
    _wiki_backlinks.add_argparse(wiki_bl_p)

    skills_p = sub.add_parser(
        "skills",
        help="Skills maintenance helpers (catalog regeneration, "
             "future lint passes).",
    )
    skills_sub = skills_p.add_subparsers(dest="skills_action")

    skills_cat_p = skills_sub.add_parser(
        "catalog",
        help="Regenerate the skills catalog page (chainlink #81 / G5) — "
             "walks SKILL.md frontmatter to produce a RESOLVER.md-style "
             "dispatcher. Default output is stdout; pass --out to write "
             "to state/wiki/topics/skills-catalog.md.",
    )
    from . import skill_catalog as _skill_catalog
    _skill_catalog.add_argparse(skills_cat_p)

    # mimir skills list — installed skills in a home
    from . import skill_install as _skill_install
    skills_list_p = skills_sub.add_parser(
        "list",
        help="List skills installed in an agent home "
             "(walks <home>/skills/). Shows name, [poller] flag "
             "when a pollers.json is present, and the SKILL.md description.",
    )
    _skill_install.add_argparse_list(skills_list_p)

    # mimir skills list-optional — opt-in skills shipped with mimir
    skills_list_opt_p = skills_sub.add_parser(
        "list-optional",
        help="List skills available for opt-in install (walks "
             "<repo>/optional-skills/). Use `mimir skills install <name>` "
             "to copy one into an agent home.",
    )
    _skill_install.add_argparse_list_optional(skills_list_opt_p)

    # mimir skills install — copy an opt-in skill into an agent home
    skills_install_p = skills_sub.add_parser(
        "install",
        help="Install an opt-in skill from optional-skills/ into an agent "
             "home's skills/. Pollers (skills with pollers.json) "
             "register on next `reload_pollers` or `mimir run` boot.",
    )
    _skill_install.add_argparse_install(skills_install_p)

    # mimir skills update — detect drift between installed and source skills
    skills_update_p = skills_sub.add_parser(
        "update",
        help="Compare installed optional skills against their source counterparts "
             "and report drift (dry-run by default). Exits 0 when all skills are "
             "up-to-date, 1 when drift is found (CI-friendly). Pass a skill name "
             "to check only that skill; omit to check all installed skills.",
    )
    _skill_install.add_argparse_update(skills_update_p)

    # mimir skills configure — interactive env-var prompts for installed skills
    skills_configure_p = skills_sub.add_parser(
        "configure",
        help="Interactively prompt for env vars declared in a skill's SKILL.md "
             "and write them to <home>/.env. Works for bundled built-in skills "
             "(weather, ntfy, …) as well as optional installed skills. "
             "Pass a skill name to configure one skill, or --all to iterate "
             "over every skill that has an env: block.",
    )
    _skill_install.add_argparse_configure(skills_configure_p)

    # mimir scaffold-docker — generate container-deploy files
    # (Dockerfile, compose.yml, compose.env, start.sh) into an agent
    # home. Inspects <home>/skills/ for per-skill OS-deps
    # (dockerfile.fragment) and env vars (pollers.json pass_env).
    # Idempotent — re-run after installing pollers to pick up their
    # fragments + env-var requirements.
    scaffold_docker_p = sub.add_parser(
        "scaffold-docker",
        help="Generate Dockerfile + compose.yml + compose.env + start.sh "
             "into an agent home. Pulls per-skill dockerfile.fragment "
             "snippets so installed pollers' system deps (gog, social-cli, "
             "etc.) get baked into the image. Idempotent — re-run after "
             "skills change to refresh.",
    )
    from . import scaffold_docker as _scaffold_docker
    _scaffold_docker.add_argparse(scaffold_docker_p)

    commitments_p = sub.add_parser(
        "commitments",
        help="Manage durable commitments (list/add/complete/snooze/"
             "dismiss/trim). Phase 1 = operator-driven; extraction + "
             "surfacing land in Phase 2/3.",
    )
    from .commitments import cli as _commitments_cli
    _commitments_cli.add_argparse(commitments_p)

    # ``mimir feedback mark-resolved`` — writer side of resolved-incidents.jsonl
    # (chainlink #197 shipped the consumer; chainlink #198 ships this writer).
    # Lets operators mark a known error pattern as resolved without hand-crafting
    # JSONL or risking timestamp format mismatch.
    feedback_p = sub.add_parser(
        "feedback",
        help="Feedback observability helpers (mark-resolved, emit).",
    )
    feedback_sub = feedback_p.add_subparsers(dest="feedback_action")

    feedback_mr_p = feedback_sub.add_parser(
        "mark-resolved",
        help="Append a resolved-incident rule to resolved-incidents.jsonl so matching "
             "events are silenced from the algedonic feedback block.",
    )
    feedback_mr_p.add_argument(
        "--type", required=True, dest="event_type",
        help="Event type to suppress, or '*' to match any type.",
    )
    feedback_mr_p.add_argument(
        "--pattern", default="",
        help="Substring to match against the event JSON (empty = match all events "
             "of the given type).",
    )
    feedback_mr_p.add_argument(
        "--reason", required=True,
        help="Free-text rationale for marking resolved (stored in the JSONL line).",
    )
    feedback_mr_p.add_argument(
        "--resolved-at", default=None, dest="resolved_at",
        help="ISO-8601 timestamp the fix landed (default: now() UTC).  Suppresses "
             "events timestamped *before* this value.",
    )
    feedback_mr_p.add_argument(
        "--dry-run", action="store_true", dest="dry_run",
        help="Preview how many events in the current 24h window would be filtered; "
             "don't write to resolved-incidents.jsonl.",
    )
    feedback_mr_p.add_argument(
        "--home", type=Path, default=None,
        help="Agent home (overrides MIMIR_HOME; default: cwd).",
    )

    # ``mimir feedback emit`` -- write a structured event from a subprocess
    # (chainlink #218). Lets Bash-side skill code emit auditable events without
    # touching Python internals or requiring an in-process logger singleton.
    feedback_emit_p = feedback_sub.add_parser(
        "emit",
        help="Write a structured event to events.jsonl.  Useful for Bash-side skill "
             "code that wants to emit auditable events without touching Python internals.",
    )
    feedback_emit_p.add_argument(
        "event_type",
        help="Event type to emit (e.g. 'pr_merge_blocked_by_changes_requested').",
    )
    feedback_emit_p.add_argument(
        "pairs",
        nargs="*",
        metavar="KEY=VALUE",
        help="Optional key=value payload fields.  Values are stored as strings "
             "by default; use --json-values to JSON-parse them.",
    )
    feedback_emit_p.add_argument(
        "--home", type=Path, default=None,
        help="Agent home (overrides MIMIR_HOME; default: cwd).",
    )
    feedback_emit_p.add_argument(
        "--json-values",
        action="store_true",
        dest="json_values",
        default=False,
        help="JSON-parse each KEY=VALUE value. Lets you pass structured data: "
             "blocking_reviewers='[\"alice\",\"bob\"]' pr=42",
    )

    reindex_p = sub.add_parser(
        "reindex",
        help="Re-embed saga atoms and/or file_search chunks under the "
             "currently-configured embedding provider. Use after "
             "switching providers (e.g. mimir setup --embedding voyage) "
             "to migrate existing data into the new vector space. "
             "Dry-run by default; pass --apply to actually write.",
    )
    from . import reindex as _reindex
    _reindex.add_argparse(reindex_p)

    # ``mimir migrate-memory`` — port saga.db (or MSAM snapshot) into
    # the new mimir.saga.db schema. The new memory subsystem
    # (mimir.saga.*) is a clean-room rewrite that drops saga's
    # age-anchored retrievability / one-way state machine / consolidation-
    # halves-stability bugs. This subcommand carries the existing atom
    # corpus + access history forward without re-encoding from scratch.
    migrate_p = sub.add_parser(
        "migrate-memory",
        help="Migrate saga.db (or MSAM snapshot) to mimir.saga.db. "
             "One-way; new DB lives alongside the old until cutover.",
    )
    migrate_p.add_argument(
        "--source", type=Path, required=True,
        help="Path to saga.db or MSAM snapshot",
    )
    migrate_p.add_argument(
        "--dest", type=Path, required=True,
        help="Output mimir.saga.db (won't overwrite without --force)",
    )
    migrate_p.add_argument(
        "--force", action="store_true",
        help="Overwrite --dest if it exists",
    )

    refl_lp_p = refl_sub.add_parser(
        "list-pending",
        help="List pending proposals from state/proposed-changes.md "
             "(numbered in chronological order).",
    )
    refl_lp_p.add_argument(
        "--json", dest="json_output", action="store_true",
        help="Emit JSON array of {num, heading, excerpt} objects.",
    )
    refl_lp_p.add_argument(
        "--home", type=Path, default=None,
        help="Agent home (overrides MIMIR_HOME; default: cwd).",
    )

    refl_resolve_p = refl_sub.add_parser(
        "resolve",
        help="Apply operator accept/reject decisions to pending proposals. "
             "Example: resolve \"accept 1 3 / reject 2 'not now'\"",
    )
    refl_resolve_p.add_argument(
        "decision_string",
        help="Accept/reject string, e.g. \"accept 1 3\" or "
             "\"accept 1 / reject 2 'reason'\".",
    )
    refl_resolve_p.add_argument(
        "--home", type=Path, default=None,
        help="Agent home (overrides MIMIR_HOME; default: cwd).",
    )

    refl_audit_p = refl_sub.add_parser(
        "audit",
        help="Print the '## Effects of prior proposals' block — "
             "predicted vs measured signals for proposals applied 1-4 weeks ago.",
    )
    refl_audit_p.add_argument(
        "--weeks-back-min", type=int, default=1,
        help="Inclusive newest age in weeks (default 1).",
    )
    refl_audit_p.add_argument(
        "--weeks-back-max", type=int, default=4,
        help="Inclusive oldest age in weeks (default 4).",
    )
    refl_audit_p.add_argument(
        "--window-days", type=int, default=7,
        help="Before/after measurement window per proposal (default 7).",
    )
    refl_audit_p.add_argument(
        "--home", type=Path, default=None,
        help="Agent home (overrides MIMIR_HOME; default: cwd).",
    )

    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "setup":
        status = setup_home(
            args.home,
            embedding=args.embedding,
            model=args.model,
            subscription=args.subscription,
        )
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

    if args.command == "stats":
        from .config import Config as _Config
        from .rate_limits import RateLimitStore
        from .stats_block import assemble_stats_block
        home_arg = args.home or os.environ.get("MIMIR_HOME") or Path.cwd()
        os.environ["MIMIR_HOME"] = str(Path(home_arg).resolve())
        cfg = _Config.from_env()
        store = RateLimitStore(path=cfg.home / ".mimir" / "rate_limits.json")
        # ``assemble_stats_block`` is the shared assembly used on the
        # agent loop too (mimir/stats_block.py). CLI passes the
        # ``RateLimitStore`` itself (not the .current() dict) so the
        # helper can call .current() inside its own try/except and
        # degrade gracefully on a corrupt rate_limits.json instead
        # of nuking the whole block. No JsonlSnapshot — one-shot use,
        # no caching wins. ``betas`` defaults from ``cfg.context_1m``
        # so the CLI output's context-window arithmetic matches what
        # the agent renders.
        result = assemble_stats_block(cfg, store)
        if result.body is None:
            print("(no turns recorded yet)")
        else:
            print(result.body)
        alert = result.alert
        # CR2 (ops & observability) fix: also print the billing mode
        # and which event the agent WOULD emit for the alert, so an
        # operator triaging "did the agent see an alert?" gets a
        # diagnostic that mirrors the agent's actual decision.
        # Pre-fix, ``mimir stats`` skipped billing-mode evaluation
        # entirely — a quota-mode install with the alert tripped
        # showed identical output to a pay-as-you-go install,
        # because the agent's advisory-vs-alert distinction was
        # absent here.
        from .billing import detect_billing_mode, BillingMode
        from .config import _oauth_credentials_path
        oauth_path = _oauth_credentials_path()
        billing_mode = detect_billing_mode(
            explicit=os.environ.get("MIMIR_BILLING_MODE") or None,
            oauth_credentials_path=oauth_path,
        )
        print(f"\nBilling mode (auto-detected): {billing_mode.value}")
        if alert is not None:
            event_name = (
                "cost_rate_advisory"
                if billing_mode == BillingMode.QUOTA
                else "cost_rate_alert"
            )
            print(
                f"On the agent loop, this would emit: {event_name} "
                f"(reason={alert.reason})"
            )
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

    if args.command == "verify-cred":
        from .cred_verify import run_verify_cred_cmd
        sys.exit(run_verify_cred_cmd(args.name))

    if args.command == "verify-creds":
        from .cred_verify import run_verify_creds_cmd
        sys.exit(run_verify_creds_cmd(only_type=args.cred_type))

    if args.command == "verify-index":
        from .index_integrity import run_verify_index_cmd
        home = (args.home or Path(os.environ.get("MIMIR_HOME") or Path.cwd())).resolve()
        sys.exit(run_verify_index_cmd(home=home, db=args.db))

    if args.command == "viability-report":
        from .viability_metrics import run_viability_report_cmd
        home = (args.home or Path(os.environ.get("MIMIR_HOME") or Path.cwd())).resolve()
        warning_count = run_viability_report_cmd(
            home=home,
            collapse_window_days=args.collapse_window_days,
            curation_window_days=args.curation_window_days,
            write_to_disk=not args.no_write,
        )
        # Exit 0 for clean (no warnings), 1 for one-or-more warnings.
        # Mirrors verify-index / verify-cred so operator scripts can
        # branch on the count.
        sys.exit(0 if warning_count == 0 else 1)

    if args.command == "rotate":
        from .cred_rotate import run_rotate
        new_value: str | None = None
        if args.from_file:
            try:
                new_value = Path(args.from_file).read_text(encoding="utf-8").rstrip("\n")
            except OSError as exc:
                print(f"can't read --from-file: {exc}", file=sys.stderr)
                sys.exit(2)
        dep = Path(args.deployment_dir).resolve() if args.deployment_dir else None
        sys.exit(run_rotate(
            env_name=args.env_name,
            new_value=new_value,
            deployment_dir=dep,
            service=args.service,
            skip_recreate=args.skip_recreate,
        ))

    if args.command == "loops":
        from .loops_cmd import run_loops_cmd
        home = (args.home or Path(os.environ.get("MIMIR_HOME") or Path.cwd())).resolve()
        sys.exit(run_loops_cmd(home))

    if args.command == "update":
        sys.exit(_cmd_update(args))

    if args.command == "predictions":
        from .skills.predictions import script as _predictions_script
        sys.exit(_predictions_script.run(args))

    if args.command == "wiki":
        if args.wiki_action == "backlinks":
            from . import wiki_backlinks as _wiki_backlinks
            sys.exit(_wiki_backlinks.cmd_backlinks(args))
        wiki_p.print_help()
        sys.exit(1)

    if args.command == "skills":
        if args.skills_action == "catalog":
            from . import skill_catalog as _skill_catalog
            sys.exit(_skill_catalog.cmd(args))
        # list / list-optional / install all set ``skill_install_cmd``
        # via their respective ``add_argparse_*`` helpers in
        # ``mimir.skill_install``.
        skill_install_cmd = getattr(args, "skill_install_cmd", None)
        if skill_install_cmd is not None:
            sys.exit(skill_install_cmd(args))
        skills_p.print_help()
        sys.exit(1)

    if args.command == "scaffold-docker":
        # _scaffold_docker was already imported at subparser-registration
        # time (parser.add_argument requires the module to wire ``cmd``).
        # Reuse the existing scaffold_docker_cmd reference set via
        # ``parser.set_defaults`` rather than re-importing here.
        sys.exit(args.scaffold_docker_cmd(args))

    if args.command == "commitments":
        # chainlink #82 sub #87: bare ``mimir commitments`` (no
        # subcommand) prints the parent parser's full ``--help`` and
        # exits 1, matching the discovery-friendly shape established
        # by identities/wiki/skills/reflection above. Argparse sends
        # ``print_help()`` to stdout so the help is pipeline-friendly
        # (greppable, redirectable); the non-zero exit signals "no
        # action taken" for ``mimir <something> || handle_error``
        # callers — uniform with the sibling subcommands.
        if args.commitments_action is None:
            commitments_p.print_help()
            sys.exit(1)
        from .commitments import cli as _commitments_cli
        sys.exit(_commitments_cli.dispatch(args))

    if args.command == "feedback":
        if args.feedback_action == "mark-resolved":
            from .feedback_cmd import run_mark_resolved
            home = (args.home or Path(os.environ.get("MIMIR_HOME") or Path.cwd())).resolve()
            sys.exit(run_mark_resolved(
                home=home,
                event_type=args.event_type,
                pattern=args.pattern,
                reason=args.reason,
                resolved_at=args.resolved_at,
                dry_run=args.dry_run,
            ))
        if args.feedback_action == "emit":
            from .feedback_cmd import run_emit_event
            home = (args.home or Path(os.environ.get("MIMIR_HOME") or Path.cwd())).resolve()
            sys.exit(run_emit_event(
                home=home,
                event_type=args.event_type,
                pairs=args.pairs,
                json_values=getattr(args, "json_values", False),
            ))
        feedback_p.print_help()
        sys.exit(1)

    if args.command == "reindex":
        from . import reindex as _reindex
        sys.exit(_reindex.dispatch(args))

    if args.command == "migrate-memory":
        from .saga.migrate import migrate as _migrate_memory
        try:
            _migrate_memory(
                source=args.source,
                dest=args.dest,
                force=args.force,
                log=print,
            )
        except FileExistsError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)

    if args.command == "reflection":
        if args.reflection_action == "most-retrieved":
            from .reflection import most_retrieved as _most_retrieved
            sys.exit(asyncio.run(_most_retrieved.run(args)))
        if args.reflection_action == "introspection-report":
            from .reflection import introspection_report as _intro_report
            sys.exit(_intro_report.run(args))
        if args.reflection_action == "mark-applied":
            from .reflection import applied_audit as _applied_audit
            home = (args.home or Path(os.environ.get("MIMIR_HOME") or Path.cwd())).resolve()
            try:
                proposal = _applied_audit.mark_applied(
                    home / "state" / "proposed-changes.md",
                    home / "state" / "applied-proposals.jsonl",
                    args.id_match,
                )
            except (FileNotFoundError, LookupError, ValueError) as exc:
                print(f"mark-applied: {exc}", file=sys.stderr)
                sys.exit(1)
            print(f"Applied: {proposal.id}")
            sys.exit(0)
        if args.reflection_action == "list-pending":
            import json as _json
            from .reflection import applied_audit as _applied_audit
            home = (args.home or Path(os.environ.get("MIMIR_HOME") or Path.cwd())).resolve()
            try:
                proposals = _applied_audit._list_pending_proposals(
                    home / "state" / "proposed-changes.md"
                )
            except FileNotFoundError as exc:
                print(f"list-pending: {exc}", file=sys.stderr)
                sys.exit(1)
            except ValueError as exc:
                print(f"list-pending: {exc}", file=sys.stderr)
                sys.exit(1)
            if not proposals:
                if getattr(args, "json_output", False):
                    print("[]")
                else:
                    print("0 pending proposals")
                sys.exit(0)
            if getattr(args, "json_output", False):
                print(_json.dumps(
                    [{"num": n, "heading": h, "excerpt": e}
                     for n, h, e in proposals],
                    ensure_ascii=False,
                ))
            else:
                for num, heading, excerpt in proposals:
                    line = f"{num}. {heading}"
                    if excerpt:
                        line += f"\n   {excerpt}"
                    print(line)
            sys.exit(0)
        if args.reflection_action == "resolve":
            from .reflection import applied_audit as _applied_audit
            home = (args.home or Path(os.environ.get("MIMIR_HOME") or Path.cwd())).resolve()
            pc_path = home / "state" / "proposed-changes.md"
            log_path = home / "state" / "applied-proposals.jsonl"

            try:
                ops = _applied_audit.parse_resolve_string(args.decision_string)
            except ValueError as exc:
                print(f"resolve: {exc}", file=sys.stderr)
                sys.exit(1)

            # Number proposals once — snapshot before any mutation.
            try:
                snapshot = _applied_audit._list_pending_proposals(pc_path)
            except FileNotFoundError as exc:
                print(f"resolve: {exc}", file=sys.stderr)
                sys.exit(1)
            except ValueError as exc:
                print(f"resolve: {exc}", file=sys.stderr)
                sys.exit(1)

            # Resolve all (action, num) → heading before mutating so that
            # numbering shifts after earlier mutations don't affect later ones.
            resolved: list[tuple[str, str, str]] = []  # (action, heading, reason)
            errors: list[str] = []
            for action, num, reason in ops:
                match = next((h for n, h, _ in snapshot if n == num), None)
                if match is None:
                    errors.append(
                        f"  {num}: out of range (1–{len(snapshot)})"
                        if snapshot else f"  {num}: no pending proposals"
                    )
                    continue
                resolved.append((action, match, reason))

            accepted: list[str] = []
            rejected: list[str] = []

            for action, heading, reason in resolved:
                if action == "accept":
                    try:
                        _applied_audit.mark_applied(pc_path, log_path, heading)
                        # Find original num for output label.
                        num_label = next(
                            str(n) for n, h, _ in snapshot if h == heading
                        )
                        accepted.append(num_label)
                    except (LookupError, ValueError) as exc:
                        errors.append(f"  {heading!r}: {exc}")
                else:
                    default_reason = "operator declined"
                    effective_reason = reason.strip() if reason.strip() else default_reason
                    try:
                        _applied_audit.mark_reject(pc_path, heading, effective_reason)
                        num_label = next(
                            str(n) for n, h, _ in snapshot if h == heading
                        )
                        rejected.append(f"{num_label} ({effective_reason!r})")
                    except (LookupError, ValueError) as exc:
                        errors.append(f"  {heading!r}: {exc}")

            parts = []
            if accepted:
                parts.append(f"Applied: {', '.join(accepted)}.")
            if rejected:
                parts.append(f"Rejected: {', '.join(rejected)}.")
            if errors:
                parts.append("Errors:\n" + "\n".join(errors))
            print("\n".join(parts) if parts else "Nothing to do.")
            sys.exit(1 if errors and not accepted and not rejected else 0)
        if args.reflection_action == "audit":
            from .reflection import applied_audit as _applied_audit
            home = (args.home or Path(os.environ.get("MIMIR_HOME") or Path.cwd())).resolve()
            rows = _applied_audit.audit_window(
                home,
                weeks_back_min=args.weeks_back_min,
                weeks_back_max=args.weeks_back_max,
                window_days=args.window_days,
            )
            block = _applied_audit.render_audit_block(rows)
            if block is None:
                print(
                    f"(no proposals applied {args.weeks_back_max}–"
                    f"{args.weeks_back_min} weeks ago)"
                )
            else:
                print("## Effects of prior proposals\n")
                print(block)
            sys.exit(0)
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
