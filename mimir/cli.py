"""Command-line entrypoint for mimir.

Subcommands:
- ``mimir setup [--home DIR]`` — scaffold an agent home (dirs, .env template,
  scheduler.yaml stub, skills, subagents). Idempotent — never overwrites
  existing files.
- ``mimir run [--home DIR]``   — run the server (default if no subcommand).
- ``mimir identities {list,add,remove,resolve}`` — manage identity
  reconciliation entries (FUTURE_WORK §6.1).

Both run/setup commands export ``MIMIR_HOME`` to the resolved path before
loading ``Config.from_env()``, so the CLI flag and the env var converge.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Sequence

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

# Re-exports from commands.identities (backward compatibility for any
# callers that import the private helpers from mimir.cli).
from .commands.identities import (  # noqa: E402
    _identities_load,
    _identities_save,
    _identities_list_cmd,
    _identities_add_cmd,
    _identities_remove_cmd,
    _identities_resolve_cmd,
)


# ---------------------------------------------------------------------------
# ``mimir update`` implementation (thin; no separate module warranted yet)
# ---------------------------------------------------------------------------


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

    # `mimir identities {list,add,remove,resolve}` — delegate to commands.identities
    from .commands import identities as _identities_cmd
    id_p = _identities_cmd.add_argparse(sub)

    # `mimir reflection <action>` — delegate to commands.reflection
    from .commands import reflection as _reflection_cmd
    refl_p = _reflection_cmd.add_argparse(sub)

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

    # `mimir stats` — operator-facing usage report.
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

    # `mimir verify-cred` / `mimir verify-creds`
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

    # ``mimir viability-report``
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

    # ``mimir verify-index``
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

    # ``mimir rotate``
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

    # ``mimir update``
    update_p = sub.add_parser(
        "update",
        help="Check PyPI for a newer mimir release; with --apply, install it via pip.",
    )
    update_p.add_argument(
        "--check",
        action="store_true",
        help=(
            "Print status only; exit non-zero when an update is available "
            "(useful in scripts / CI)."
        ),
    )
    update_p.add_argument(
        "--apply",
        action="store_true",
        help=(
            "If a newer version is on PyPI, run `python -m pip install --upgrade mimir`. "
            "You must restart the agent (e.g., `docker compose restart`) to engage "
            "the new code."
        ),
    )
    update_p.add_argument(
        "--pre",
        action="store_true",
        help="Include pre-release versions (alpha / beta / rc / dev) when checking.",
    )

    pred_p = sub.add_parser(
        "predictions",
        help="Predictions tracking CLI (skills/predictions/script.py).",
    )
    from .skills.predictions import script as _predictions_script
    _predictions_script.add_argparse(pred_p)

    # `mimir wiki <action>`
    wiki_p = sub.add_parser(
        "wiki",
        help="Wiki maintenance helpers (backlinks, future lint passes).",
    )
    wiki_sub = wiki_p.add_subparsers(dest="wiki_action")
    wiki_bl_p = wiki_sub.add_parser(
        "backlinks",
        help=(
            "Walk state/wiki/, write orphans.md / dangling-links.md / "
            "backlinks-index.md. Emits wiki_backlinks_unhealthy event "
            "when the wiki has orphans or dangling links."
        ),
    )
    from . import wiki_backlinks as _wiki_backlinks
    _wiki_backlinks.add_argparse(wiki_bl_p)

    # `mimir skills <action>`
    skills_p = sub.add_parser(
        "skills",
        help="Skills maintenance helpers (catalog regeneration, future lint passes).",
    )
    skills_sub = skills_p.add_subparsers(dest="skills_action")
    skills_cat_p = skills_sub.add_parser(
        "catalog",
        help=(
            "Regenerate the skills catalog page (chainlink #81 / G5) — "
            "walks SKILL.md frontmatter to produce a RESOLVER.md-style "
            "dispatcher. Default output is stdout; pass --out to write "
            "to state/wiki/topics/skills-catalog.md."
        ),
    )
    from . import skill_catalog as _skill_catalog
    _skill_catalog.add_argparse(skills_cat_p)

    from . import skill_install as _skill_install
    skills_list_p = skills_sub.add_parser(
        "list",
        help=(
            "List skills installed in an agent home "
            "(walks <home>/skills/). Shows name, [poller] flag "
            "when a pollers.json is present, and the SKILL.md description."
        ),
    )
    _skill_install.add_argparse_list(skills_list_p)

    skills_list_opt_p = skills_sub.add_parser(
        "list-optional",
        help=(
            "List skills available for opt-in install (walks "
            "<repo>/optional-skills/). Use `mimir skills install <name>` "
            "to copy one into an agent home."
        ),
    )
    _skill_install.add_argparse_list_optional(skills_list_opt_p)

    skills_install_p = skills_sub.add_parser(
        "install",
        help=(
            "Install an opt-in skill from optional-skills/ into an agent "
            "home's skills/. Pollers (skills with pollers.json) "
            "register on next `reload_pollers` or `mimir run` boot."
        ),
    )
    _skill_install.add_argparse_install(skills_install_p)

    skills_update_p = skills_sub.add_parser(
        "update",
        help=(
            "Compare installed optional skills against their source counterparts "
            "and report drift (dry-run by default). Exits 0 when all skills are "
            "up-to-date, 1 when drift is found (CI-friendly). Pass a skill name "
            "to check only that skill; omit to check all installed skills."
        ),
    )
    _skill_install.add_argparse_update(skills_update_p)

    skills_configure_p = skills_sub.add_parser(
        "configure",
        help=(
            "Interactively prompt for env vars declared in a skill's SKILL.md "
            "and write them to <home>/.env. Works for bundled built-in skills "
            "(weather, ntfy, …) as well as optional installed skills. "
            "Pass a skill name to configure one skill, or --all to iterate "
            "over every skill that has an env: block."
        ),
    )
    _skill_install.add_argparse_configure(skills_configure_p)

    # `mimir scaffold-docker`
    scaffold_docker_p = sub.add_parser(
        "scaffold-docker",
        help=(
            "Generate Dockerfile + compose.yml + compose.env + start.sh "
            "into an agent home. Pulls per-skill dockerfile.fragment "
            "snippets so installed pollers' system deps (gog, social-cli, "
            "etc.) get baked into the image. Idempotent — re-run after "
            "skills change to refresh."
        ),
    )
    from . import scaffold_docker as _scaffold_docker
    _scaffold_docker.add_argparse(scaffold_docker_p)

    # `mimir commitments`
    commitments_p = sub.add_parser(
        "commitments",
        help=(
            "Manage durable commitments (list/add/complete/snooze/"
            "dismiss/trim). Phase 1 = operator-driven; extraction + "
            "surfacing land in Phase 2/3."
        ),
    )
    from .commitments import cli as _commitments_cli
    _commitments_cli.add_argparse(commitments_p)

    # `mimir feedback {mark-resolved,emit}` — delegate to commands.feedback
    from .commands import feedback as _feedback_cmd
    feedback_p = _feedback_cmd.add_argparse(sub)

    # `mimir reindex`
    reindex_p = sub.add_parser(
        "reindex",
        help=(
            "Re-embed saga atoms and/or file_search chunks under the "
            "currently-configured embedding provider. Use after "
            "switching providers (e.g. mimir setup --embedding voyage) "
            "to migrate existing data into the new vector space. "
            "Dry-run by default; pass --apply to actually write."
        ),
    )
    from . import reindex as _reindex
    _reindex.add_argparse(reindex_p)

    # ``mimir migrate-memory``
    migrate_p = sub.add_parser(
        "migrate-memory",
        help=(
            "Migrate saga.db (or MSAM snapshot) to mimir.saga.db. "
            "One-way; new DB lives alongside the old until cutover."
        ),
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

    # -----------------------------------------------------------------------
    # Dispatch
    # -----------------------------------------------------------------------

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
        code = _identities_cmd.dispatch(args, id_p)
        if code:
            sys.exit(code)
        return

    if args.command == "stats":
        from .config import Config as _Config
        from .rate_limits import RateLimitStore
        from .stats_block import assemble_stats_block
        home_arg = args.home or os.environ.get("MIMIR_HOME") or Path.cwd()
        os.environ["MIMIR_HOME"] = str(Path(home_arg).resolve())
        cfg = _Config.from_env()
        store = RateLimitStore(path=cfg.home / ".mimir" / "rate_limits.json")
        result = assemble_stats_block(cfg, store)
        if result.body is None:
            print("(no turns recorded yet)")
        else:
            print(result.body)
        alert = result.alert
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
        # list / list-optional / install / update / configure all set
        # ``skill_install_cmd`` via their respective ``add_argparse_*``
        # helpers in ``mimir.skill_install``.
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
        if args.commitments_action is None:
            commitments_p.print_help()
            sys.exit(1)
        from .commitments import cli as _commitments_cli
        sys.exit(_commitments_cli.dispatch(args))

    if args.command == "feedback":
        sys.exit(_feedback_cmd.dispatch(args, feedback_p))

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
        sys.exit(_reflection_cmd.dispatch(args, refl_p))

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
