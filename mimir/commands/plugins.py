"""Metadata-only listing of installed plugins."""

from __future__ import annotations

import argparse
import json
import os

from ..plugins import discover_plugins


def add_argparse(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    plugins_p = sub.add_parser("plugins", help="Inspect installed plugins.")
    plugins_sub = plugins_p.add_subparsers(dest="plugins_action")
    list_p = plugins_sub.add_parser("list", help="List installed plugin metadata.")
    list_p.add_argument("--json", action="store_true", dest="as_json", help="Emit JSON instead of text.")
    docker_p = plugins_sub.add_parser("dockerfile", help="Print installed plugin Dockerfile fragments.")
    docker_p.add_argument("--stage", choices=("base", "build", "runtime", "services"))
    docker_p.add_argument("--all-installed", action="store_true",
                          help="Include disabled installed plugins as well as enabled ones.")
    return plugins_p


def dispatch(args: argparse.Namespace, plugins_p: argparse.ArgumentParser) -> int:
    if args.plugins_action == "dockerfile":
        from ..plugin_dockerfile import render_stage, select_plugins

        candidates = select_plugins(all_installed=args.all_installed,
                                    enable=os.environ.get("MIMIR_PLUGINS", ""))
        stages = (args.stage,) if args.stage else ("base", "build", "runtime", "services")
        output = "\n\n".join(fragment for stage in stages
                             if (fragment := render_stage(candidates, stage)))
        if output:
            print(output)
        return 0
    if args.plugins_action != "list":
        plugins_p.print_help()
        return 1

    # Capture process selection explicitly: no Config or agent-home dotenv belongs
    # on this inspection path, and discovery must never load an entry point.
    candidates = discover_plugins(enable=os.environ.get("MIMIR_PLUGINS", ""))
    rows = [
        {
            "name": candidate.name,
            "version": candidate.version,
            "license": candidate.license,
            "distribution": candidate.distribution,
            "enabled": candidate.enabled,
            "diagnostic": candidate.diagnostic,
        }
        for candidate in candidates
    ]
    if args.as_json:
        print(json.dumps(rows, indent=2))
    elif not rows:
        print("No plugins installed.")
    else:
        for candidate, row in zip(candidates, rows):
            print(
                f"{row['name']} {row['version'] or '-'} ({row['license'] or '-'}) "
                f"from {row['distribution']} — {'enabled' if row['enabled'] else 'disabled'}"
            )
            if row["diagnostic"]:
                print(f"  diagnostic: {row['diagnostic']}")
            manifest = getattr(candidate, "manifest", None)
            for command, operator in (getattr(manifest, "cli_commands", None) or {}).items():
                print(f"  mimir {command} — {'operator' if operator else 'requires MIMIR_PLUGINS=' + candidate.name}")
    return 0
