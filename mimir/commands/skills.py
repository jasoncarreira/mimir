"""Skills subcommand — ``mimir skills {catalog,list,list-optional,install,update,accept,configure}``.

Extracted from ``mimir.cli`` (Phase 3, chainlink #240).
Business logic lives in ``mimir.skill_catalog`` and ``mimir.skill_install``;
this module owns the argparse tree and dispatches into them.
"""

from __future__ import annotations

import argparse
import sys


def add_argparse(sub: "argparse._SubParsersAction") -> argparse.ArgumentParser:
    """Register ``mimir skills`` subcommand tree.  Returns the created
    parser so the caller can pass it to :func:`dispatch` for ``print_help``."""
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
    from .. import skill_catalog as _skill_catalog
    _skill_catalog.add_argparse(skills_cat_p)

    from .. import skill_install as _skill_install

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
            "mimir/optional-skills/). Use `mimir skills install <name>` "
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

    skills_accept_p = skills_sub.add_parser(
        "accept",
        help=(
            "Accept current intentional optional-skill drift by recording per-file "
            "installed+source hashes. Accepted drift stays visible in update output "
            "but is suppressed from version-bump digests while the hashes match."
        ),
    )
    _skill_install.add_argparse_accept(skills_accept_p)

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

    return skills_p


def dispatch(args: argparse.Namespace, skills_p: argparse.ArgumentParser) -> int:
    """Dispatch ``mimir skills`` to the appropriate handler.  Returns an exit code."""
    if args.skills_action == "catalog":
        from .. import skill_catalog as _skill_catalog
        return _skill_catalog.cmd(args)
    # list / list-optional / install / update / configure all set
    # ``skill_install_cmd`` via their respective ``add_argparse_*``
    # helpers in ``mimir.skill_install``.
    skill_install_cmd = getattr(args, "skill_install_cmd", None)
    if skill_install_cmd is not None:
        return skill_install_cmd(args)
    skills_p.print_help()
    return 1
