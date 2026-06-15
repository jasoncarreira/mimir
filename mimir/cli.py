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
    DEFAULT_NON_GOALS,
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


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="mimir",
        description="Memory-centric agent harness on the Claude Agent SDK.",
    )
    sub = parser.add_subparsers(dest="command")

    # `mimir setup` — delegate to commands.setup
    from .commands import setup as _setup_cmd
    setup_p = _setup_cmd.add_argparse(sub)

    run_p = sub.add_parser("run", help="Run the mimir server (default).")
    run_p.add_argument(
        "--home", type=Path, default=None,
        help="Agent home (overrides MIMIR_HOME). Required: `run` refuses to "
             "start if neither --home nor MIMIR_HOME is set.",
    )

    # `mimir identities {list,add,remove,resolve}` — delegate to commands.identities
    from .commands import identities as _identities_cmd
    id_p = _identities_cmd.add_argparse(sub)

    # `mimir reflection <action>` — delegate to commands.reflection
    from .commands import reflection as _reflection_cmd
    refl_p = _reflection_cmd.add_argparse(sub)

    # `mimir memory <action>` — delegate to commands.memory (core-memory PR workflow)
    from .commands import memory as _memory_cmd
    mem_p = _memory_cmd.add_argparse(sub)

    # `mimir worklink run <issue>` — operator-invoked Worklink executor.
    from .commands import worklink as _worklink_cmd
    worklink_p = _worklink_cmd.add_argparse(sub)

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
    stats_p.add_argument(
        "--tools",
        action="store_true",
        help="Also show per-tool call/error counts from logs/events.jsonl.",
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

    # `mimir update` — delegate to commands.update
    from .commands import update as _update_cmd
    _update_cmd.add_argparse(sub)

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

    # `mimir skills <action>` — delegate to commands.skills
    from .commands import skills as _skills_cmd
    skills_p = _skills_cmd.add_argparse(sub)

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

    if args.command == "memory":
        code = _memory_cmd.dispatch(args, mem_p)
        if code:
            sys.exit(code)
        return

    if args.command == "worklink":
        sys.exit(_worklink_cmd.dispatch(args, worklink_p))

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
        if getattr(args, "tools", False):
            from .ops_dashboard import build_dashboard_payload
            payload = build_dashboard_payload(cfg.home / "logs" / "events.jsonl", days=7)
            tools = payload.get("tools") or []
            print("\nTool calls (last 7d):")
            if not tools:
                print("  (no tool_call events recorded)")
            else:
                for row in tools[:30]:
                    rate = float(row.get("failure_rate") or 0.0) * 100.0
                    avg = float(row.get("avg_duration_ms") or 0.0)
                    print(
                        f"  {row.get('tool')}: {row.get('calls', 0)} calls, "
                        f"{row.get('errors', 0)} errors ({rate:.1f}%), "
                        f"avg {avg:.0f}ms"
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
        sys.exit(_update_cmd.dispatch(args))

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
        sys.exit(_skills_cmd.dispatch(args, skills_p))

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
        # The agent home must be EXPLICIT. Refuse to start with neither
        # ``--home`` nor ``MIMIR_HOME`` rather than silently defaulting to the
        # process cwd: a cwd-home scatters state/skills/memory wherever the
        # process happened to launch and makes the agent chase container-shaped
        # paths that don't exist on the box (the classic non-Docker failure).
        if not os.environ.get("MIMIR_HOME"):
            print(
                "error: MIMIR_HOME is not set.\n"
                "  The agent home must be explicit. Either:\n"
                "    mimir run --home /path/to/agent-home\n"
                "  or export MIMIR_HOME=/path/to/agent-home before `mimir run`.\n"
                "  (Refusing to default to the current directory.)",
                file=sys.stderr,
            )
            sys.exit(1)
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
