"""Identities subcommand — ``mimir identities {list,add,remove,resolve,...}``.

Extracted from ``mimir.cli`` (Phase 2, chainlink #240).
Manages the alias map at ``<home>/state/identities.yaml``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from ..identities import IdentityResolver
from ..identities_populator import approve_pairing


# ---------------------------------------------------------------------------
# Business-logic helpers (formerly private in mimir.cli)
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


def _identities_approve_pairing_cmd(
    home: Path, identity: str, roles: list[str]
) -> None:
    if not approve_pairing(home, identity, roles=roles):
        raise ValueError(
            f"no pending identity found for {identity!r}, or it is already approved"
        )
    print(f"approved pairing: {identity} ({', '.join(roles)})")


def _identities_issue_key_cmd(
    home: Path, canonical: str, roles: list[str] | None
) -> None:
    """Mint (or rotate) a per-user web API key and print it ONCE.

    Only the SHA-256 hash is persisted to identities.yaml; the raw value below
    is the sole copy — the operator distributes it out-of-band. Re-running
    rotates (invalidates the prior key)."""
    from ..identities_populator import issue_web_key

    raw = issue_web_key(home, canonical, roles=roles)
    role_str = ", ".join(roles) if roles else "(roles unchanged)"
    print(f"issued web key for {canonical} [{role_str}] — previous key (if any) is now revoked")
    print()
    print("  ┌─ COPY NOW — shown once, not recoverable (only its hash is stored) ─")
    print(f"  │  {raw}")
    print("  └─ hand to the user over a secure out-of-band channel ─────────────")


def _identities_revoke_key_cmd(home: Path, canonical: str) -> None:
    """Drop a user's web API key (it stops working); roles are left intact."""
    from ..identities_populator import revoke_web_key

    if revoke_web_key(home, canonical):
        print(f"revoked web key for {canonical}")
    else:
        print(f"(no web key to revoke for {canonical!r})")


# ---------------------------------------------------------------------------
# argparse registration + dispatch
# ---------------------------------------------------------------------------


def add_argparse(sub: "argparse._SubParsersAction") -> argparse.ArgumentParser:
    """Register ``mimir identities`` subcommand tree.  Returns the created
    parser so the caller can pass it to :func:`dispatch` for ``print_help``."""
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
    id_add_p.add_argument(
        "--canonical", required=True, help="Canonical id (e.g. 'alice').",
    )
    id_add_p.add_argument(
        "--alias",
        required=True,
        help=(
            "Platform-prefixed alias (e.g. 'slack-U05ALICE', 'discord-456789', "
            "'bsky:alice.bsky.social', 'email:alice@example.com')."
        ),
    )
    id_add_p.add_argument("--display-name", default=None, help="Optional display name.")
    id_add_p.add_argument("--notes", default=None, help="Optional notes (surfaces in prompt).")

    id_rm_p = id_sub.add_parser(
        "remove",
        help="Remove an alias or an entire identity.",
    )
    id_rm_p.add_argument("--home", type=Path, default=Path.cwd())
    rm_group = id_rm_p.add_mutually_exclusive_group(required=True)
    rm_group.add_argument(
        "--alias", help="Alias to remove (from whichever identity owns it).",
    )
    rm_group.add_argument(
        "--canonical", help="Canonical id of an identity to remove entirely.",
    )

    id_resolve_p = id_sub.add_parser(
        "resolve",
        help="Diagnostic: show what an author id maps to.",
    )
    id_resolve_p.add_argument("--home", type=Path, default=Path.cwd())
    id_resolve_p.add_argument(
        "author", help="Author id to resolve (e.g. 'slack-U05ALICE').",
    )

    id_approve_p = id_sub.add_parser(
        "approve-pairing",
        help="Approve a pending first-contact DM pairing.",
    )
    id_approve_p.add_argument("--home", type=Path, default=Path.cwd())
    id_approve_p.add_argument(
        "identity",
        help="Canonical id or alias to approve (e.g. 'slack-U05ALICE').",
    )
    id_approve_p.add_argument(
        "--admin",
        action="store_true",
        help="Grant both user and admin roles instead of user only.",
    )

    id_issue_p = id_sub.add_parser(
        "issue-key",
        help="Mint or rotate a per-user web API key (printed once; only its hash is stored).",
    )
    id_issue_p.add_argument("--home", type=Path, default=Path.cwd())
    id_issue_p.add_argument(
        "canonical", help="Canonical id to issue the key for (created if new).",
    )
    issue_role_group = id_issue_p.add_mutually_exclusive_group()
    issue_role_group.add_argument(
        "--admin", action="store_true", help="Grant user+admin roles.",
    )
    issue_role_group.add_argument(
        "--rotate-only",
        action="store_true",
        help="Rotate the key without changing the user's existing roles.",
    )

    id_revoke_p = id_sub.add_parser(
        "revoke-key",
        help="Revoke a user's web API key (the key stops working; roles are left intact).",
    )
    id_revoke_p.add_argument("--home", type=Path, default=Path.cwd())
    id_revoke_p.add_argument(
        "canonical", help="Canonical id whose web key to revoke.",
    )

    return id_p


def dispatch(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Handle ``mimir identities …`` dispatch.  Returns an exit code."""
    if args.identities_action is None:
        parser.print_help()
        return 1
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
        elif args.identities_action == "approve-pairing":
            roles = ["user", "admin"] if args.admin else ["user"]
            _identities_approve_pairing_cmd(home, args.identity, roles)
        elif args.identities_action == "issue-key":
            roles = None if args.rotate_only else (
                ["user", "admin"] if args.admin else ["user"]
            )
            _identities_issue_key_cmd(home, args.canonical, roles)
        elif args.identities_action == "revoke-key":
            _identities_revoke_key_cmd(home, args.canonical)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0
