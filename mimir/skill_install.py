"""Install opt-in skills from ``optional-skills/`` into an agent home.

Background
----------
``mimir setup`` seeds the always-on skills (the 30-or-so bundled under
``mimir/skills/``). Beyond that, a few skill bundles ship under
``optional-skills/`` at the repo root — pollers and integrations that
most installs don't want by default because they require external
services / credentials (Gmail OAuth, GitHub PAT, Bluesky session
tokens, etc.).

Wiring those in used to mean ``cp -r mimir/optional-skills/<name>
<home>/skills/`` plus reading the SKILL.md for env vars to set.
This module makes it a CLI: ``mimir skills install <name>`` and
``mimir skills list-optional``.

The skill bundle stays in this repo; install just copies the directory
to the agent's ``skills/`` so the runtime's skill discovery
picks it up. The agent's next ``reload_pollers`` (or next ``mimir run``
boot) registers the poller defined in ``pollers.json``.

Layout assumptions
------------------
* Source: ``<repo_root>/optional-skills/<name>/`` — must contain
  ``SKILL.md``. May also contain ``pollers.json`` (poller skills) and
  any other support files (Python sources, tests, templates).
* Destination: ``<home>/skills/<name>/``. Created if absent.

A pre-existing destination is treated as a conflict; ``--force``
clobbers (after a recursive removal). The intent is "operators see
what's there before overwriting"; the CLI never silently overwrites
custom edits.
"""

from __future__ import annotations

import datetime
import hashlib
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from mimir.skill_defs import home_skills_dir
from mimir.skill_md import parse_env_block, parse_frontmatter

# Source root for optional-skills, relative to this file.
#: ``mimir/skill_install.py`` lives at ``mimir/skill_install.py`` and the
#: optional-skills bundle lives alongside it at ``mimir/optional-skills/``.
#: chainlink #299: moved under the package (was repo-root ``optional-skills/``)
#: so it ships in the wheel and ``mimir skills install`` / ``list-optional``
#: work on pip installs, not just source-tree checkouts.
DEFAULT_OPTIONAL_SKILLS_ROOT = Path(__file__).parent / "optional-skills"

#: Maximum width of the description column in CLI listings before we
#: truncate with an ellipsis. Tuned so a single line stays under ~140
#: cols when combined with name + [poller] flag.
_DESC_BUDGET = 100

#: Fixed width for the ``[poller]`` flag column in listings, so rows
#: with and without the flag stay vertically aligned (issue #226 nit 3).
#: " [poller]" is 9 chars; pad to 10 for a 1-space gutter before the
#: description column.
_FLAG_COL_WIDTH = 10


@dataclass(frozen=True)
class OptionalSkill:
    name: str
    description: str  # one-line from SKILL.md frontmatter ``description:``
    has_pollers_json: bool
    path: Path  # path on disk (source for list_available, installed for list_installed)


@dataclass(frozen=True)
class SkillEnvSpec:
    """One env-var entry from a skill's SKILL.md ``env:`` frontmatter block."""

    name: str
    description: str
    example: str
    required: bool  # True → from ``required:`` list; False → from ``optional:``
    only_if: str | None = None  # ``"VAR=value"`` — skip prompt unless condition holds


# ─── Shared inventory helper ─────────────────────────────────────────


def _walk_skills_dir(root: Path) -> list[OptionalSkill]:
    """Walk a directory of skills and return one ``OptionalSkill`` per
    valid subdir. Shared between ``list_available`` (walks
    ``optional-skills/``) and ``list_installed`` (walks
    ``<home>/skills/``) so the two listing paths stay in lockstep.

    Skipped:
    - Hidden entries (``.foo``)
    - Non-directories
    - Directories without ``SKILL.md`` (in-progress drafts, not skills)
    - Skills whose ``SKILL.md`` frontmatter fails to parse
    """
    if not root.is_dir():
        return []
    out: list[OptionalSkill] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        skill_md = entry / "SKILL.md"
        if not skill_md.is_file():
            continue
        try:
            meta = parse_frontmatter(skill_md.read_text())
        except Exception:
            continue
        # Collapse whitespace; descriptions are often multi-line folded.
        desc = " ".join((meta.get("description") or "").strip().split())
        out.append(OptionalSkill(
            name=entry.name,
            description=desc,
            has_pollers_json=(entry / "pollers.json").is_file(),
            path=entry,
        ))
    return out


def _resolve_optional_skills_root(
    explicit: Path | None = None,
) -> Path | None:
    """Return the optional-skills root if it exists on disk, or None.

    Caller code uses the None return to emit a clear "this requires a
    source-tree mimir install" message rather than silently producing
    "no optional skills available" — the latter behavior was the
    pip-install footgun Mimir's #224 re-review flagged.
    """
    root = explicit or DEFAULT_OPTIONAL_SKILLS_ROOT
    return root if root.is_dir() else None


_PIP_INSTALL_HINT = (
    "optional-skills/ not found at {expected}.\n"
    "Optional skills ship inside the mimir package now "
    "(``mimir/optional-skills/``, chainlink #299), so a missing tree usually "
    "means a broken or partial install. Reinstall mimir-agent, or run from a "
    "source tree:\n"
    "  git clone https://github.com/jasoncarreira/mimir.git\n"
    "  cd mimir && uv sync\n"
    "  uv run mimir skills list-optional"
)


# ─── Inventory ───────────────────────────────────────────────────────


def list_available(root: Path | None = None) -> list[OptionalSkill]:
    """Walk ``optional-skills/`` and return one entry per installable skill.

    Returns ``[]`` when the optional-skills tree isn't on disk (e.g.,
    wheel install). Callers wanting to distinguish "no tree" from "tree
    but no skills" should call ``_resolve_optional_skills_root`` directly.
    """
    resolved = _resolve_optional_skills_root(root)
    if resolved is None:
        return []
    return _walk_skills_dir(resolved)


# ─── Install ────────────────────────────────────────────────────────


@dataclass
class InstallResult:
    name: str
    src: Path
    dest: Path
    overwrote: bool
    pollers_registered_hint: bool
    env_vars_written: list[str] = field(default_factory=list)


def _validate_skill_name(name: str) -> None:
    """Reject skill names that would break out of the skills root via path
    traversal. The CLI takes ``name`` directly from argparse; an operator
    running ``mimir skills install --force ../../tmp/foo`` would otherwise
    have ``shutil.rmtree(dest)`` happily delete the resolved path outside
    ``<home>/skills/``. chainlink #225.

    Rejects:
    - Empty / whitespace-only.
    - Path separators (``/`` or ``\\``) anywhere.
    - Leading ``.`` (hidden + ``..`` both caught).
    - ``..`` segments anywhere (defense-in-depth — ``/`` check already
      catches the dotted-traversal forms, but explicit is better).
    """
    if not name or not name.strip():
        raise ValueError("skill name cannot be empty")
    if "/" in name or "\\" in name:
        raise ValueError(
            f"skill name cannot contain path separators: {name!r}"
        )
    if name.startswith("."):
        raise ValueError(
            f"skill name cannot start with '.': {name!r} "
            "(reserved for hidden/dot-prefixed entries; also catches '..')"
        )
    if ".." in Path(name).parts:
        raise ValueError(
            f"skill name cannot contain '..': {name!r}"
        )


def install(
    name: str,
    home: Path,
    *,
    force: bool = False,
    optional_skills_root: Path | None = None,
) -> InstallResult:
    """Copy an opt-in skill into the agent home.

    Raises:
        ValueError: ``name`` fails path-traversal validation (chainlink #225).
        FileNotFoundError: source skill doesn't exist (either the
            optional-skills root is missing entirely, or the named skill
            isn't under it).
        FileExistsError: destination exists and ``force`` is False.
    """
    _validate_skill_name(name)
    root = _resolve_optional_skills_root(optional_skills_root)
    if root is None:
        expected = optional_skills_root or DEFAULT_OPTIONAL_SKILLS_ROOT
        raise FileNotFoundError(_PIP_INSTALL_HINT.format(expected=expected))

    src = root / name
    if not src.is_dir() or not (src / "SKILL.md").is_file():
        raise FileNotFoundError(
            f"optional skill {name!r} not found under {root}. "
            f"Run `mimir skills list-optional` to see installable skills."
        )

    dest_root = home_skills_dir(home)
    dest_root.mkdir(parents=True, exist_ok=True)
    dest = dest_root / name

    # chainlink #225 belt-and-suspenders: after resolving the destination,
    # confirm it's contained within ``dest_root``. The ``_validate_skill_name``
    # check above catches obvious traversal patterns; this catches any
    # residual edge cases (symlinks resolving outside, future bugs in name
    # handling, etc.) before the destructive rmtree.
    dest_resolved = dest.resolve()
    dest_root_resolved = dest_root.resolve()
    if not dest_resolved.is_relative_to(dest_root_resolved):
        raise ValueError(
            f"resolved destination {dest_resolved} escapes skills root "
            f"{dest_root_resolved}; refusing install"
        )

    overwrote = False
    if dest.exists():
        if not force:
            raise FileExistsError(
                f"{dest} already exists. Pass --force to overwrite "
                f"(removes the existing directory recursively). Any "
                f"custom edits you made under it are lost."
            )
        shutil.rmtree(dest)
        overwrote = True

    # ``copytree`` from src → dest. Exclude __pycache__ + .pytest_cache;
    # everything else (SKILL.md, pollers.json, Python sources, tests,
    # templates) gets carried over.
    def _ignore(_dirname, names):
        return [n for n in names if n in ("__pycache__", ".pytest_cache")]

    shutil.copytree(src, dest, ignore=_ignore)

    return InstallResult(
        name=name,
        src=src,
        dest=dest,
        overwrote=overwrote,
        pollers_registered_hint=(src / "pollers.json").is_file(),
    )


# ─── Env-var configure (mimir skills install --configure) ────────────

#: Vars matching this pattern are prompted with getpass (no echo).
_SECRET_VAR_RE = re.compile(
    r"_TOKEN$|_SECRET$|_KEY$|_PASSWORD$", re.IGNORECASE
)


def _read_env_file(env_path: Path) -> dict[str, str]:
    """Parse a Docker-style ``.env`` file into a ``key → value`` dict."""
    if not env_path.is_file():
        return {}
    out: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" in stripped:
            k, _, v = stripped.partition("=")
            out[k.strip()] = v
    return out


def _write_env_var(env_path: Path, var_name: str, value: str) -> None:
    """Write or replace ``VAR_NAME=value`` in a Docker-style ``.env`` file."""
    env_path.parent.mkdir(parents=True, exist_ok=True)
    body = env_path.read_text(encoding="utf-8") if env_path.is_file() else ""
    line_re = re.compile(
        rf"^(\s*){re.escape(var_name)}\s*=.*$", re.MULTILINE
    )
    new_line = f"{var_name}={value}"
    if line_re.search(body):
        body = line_re.sub(lambda m: f"{m.group(1)}{new_line}", body, count=1)
    else:
        if body and not body.endswith("\n"):
            body += "\n"
        body += new_line + "\n"
    env_path.write_text(body, encoding="utf-8")


def read_env_specs(
    skill_path: Path,
) -> tuple[list[SkillEnvSpec], list[SkillEnvSpec]]:
    """Return ``(required, optional)`` env-var specs from a skill's SKILL.md."""
    skill_md = skill_path / "SKILL.md"
    if not skill_md.is_file():
        return [], []
    try:
        raw_required, raw_optional = parse_env_block(skill_md.read_text())
    except Exception:
        return [], []

    def _make(items: list[dict], is_required: bool) -> list[SkillEnvSpec]:
        return [
            SkillEnvSpec(
                name=item["name"],
                description=item.get("description", ""),
                example=item.get("example", ""),
                required=is_required,
                only_if=item.get("only_if"),
            )
            for item in items
        ]

    return _make(raw_required, True), _make(raw_optional, False)


def prompt_and_write_env(
    required: list[SkillEnvSpec],
    optional: list[SkillEnvSpec],
    env_path: Path,
    *,
    skill_name: str = "",
    reconfigure: bool = False,
) -> list[str]:
    """Interactively prompt for env vars and write them to ``env_path``.

    Returns the list of var names actually written.
    """
    import getpass  # lazy — only needed during interactive configure

    existing = _read_env_file(env_path)
    session_values: dict[str, str] = {}
    written: list[str] = []

    if skill_name:
        print(f"\n# ─── {skill_name} env vars ───")

    all_specs = [*required, *optional]
    for spec in all_specs:
        # Evaluate only_if condition.
        if spec.only_if:
            cond_var, _, cond_val = spec.only_if.partition("=")
            current = session_values.get(cond_var) or existing.get(cond_var, "")
            if current.strip().lower() != cond_val.strip().lower():
                continue

        # Skip if already set and not reconfiguring.
        existing_val = existing.get(spec.name, "")
        if existing_val and not reconfigure:
            print(f"  {spec.name}: (already set — use --reconfigure to change)")
            continue

        # Build prompt text.
        label = "[required]" if spec.required else "[optional]"
        lines = [f"\n{label} {spec.name}"]
        if spec.description:
            lines.append(f"  {spec.description}")
        if spec.example:
            lines.append(f"  example: {spec.example}")
        if existing_val and reconfigure:
            is_secret = bool(_SECRET_VAR_RE.search(spec.name))
            display = ("*" * min(len(existing_val), 8)) if is_secret else existing_val
            lines.append(f"  current: {display}")
        print("\n".join(lines))

        prompt_str = "  Enter value (blank to skip): "
        if _SECRET_VAR_RE.search(spec.name):
            value = getpass.getpass(prompt_str)
        else:
            value = input(prompt_str)
        value = value.strip()

        if not value:
            if spec.required:
                print(
                    f"  ⚠ {spec.name} is required but left blank — "
                    f"set it in {env_path.name} manually before using this skill."
                )
            else:
                print("  (skipped)")
            continue

        _write_env_var(env_path, spec.name, value)
        session_values[spec.name] = value
        written.append(spec.name)
        print(f"  ✓ {spec.name} written")

    return written


def run_smoke_test(dest: Path, env_path: Path | None = None) -> tuple[int, str]:
    """Run the installed poller script once and return ``(exit_code, snippet)``.

    Tries ``<python> poller.py --once`` first; if the poller doesn't accept
    ``--once``, retries without it. Returns ``(-1, 'no poller.py found')``
    when the skill has no ``poller.py``.

    Uses ``sys.executable`` (the interpreter mimir itself runs under) rather
    than a bare ``python3`` on PATH: the poller's deps live in mimir's
    environment, and a bare ``python3`` can resolve to an unrelated
    interpreter — or, under a version manager like asdf/pyenv, to a shim
    that exits 126 ("no version set") when invoked from the skill's own
    directory (no ``.tool-versions`` there).
    """
    poller = dest / "poller.py"
    if not poller.is_file():
        return -1, "no poller.py found"

    # chainlink #259: run the smoke test with a MINIMAL env — a small set
    # of process essentials plus the skill's own .env — NOT the full
    # inherited os.environ. A third-party skill's install-time smoke test
    # has no business seeing mimir's unrelated secrets (ANTHROPIC_API_KEY,
    # SLACK_BOT_TOKEN, GITHUB_TOKEN, ...); its declared/configured vars
    # arrive via .env (env_path). The essentials keep the interpreter +
    # any tools it shells out to functional (PATH, locale, tempdir; the
    # Windows-only names are harmless no-ops elsewhere).
    _SMOKE_ENV_ESSENTIALS = (
        "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
        "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "PATHEXT", "COMSPEC",
    )
    env = {k: os.environ[k] for k in _SMOKE_ENV_ESSENTIALS if k in os.environ}
    if env_path and env_path.is_file():
        for k, v in _read_env_file(env_path).items():
            env[k] = v  # .env is authoritative for the smoke-test

    result: subprocess.CompletedProcess[str] | None = None
    for args in (["--once"], []):
        try:
            result = subprocess.run(
                [sys.executable, str(poller), *args],
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
                cwd=str(dest),
            )
        except subprocess.TimeoutExpired:
            return 124, "(smoke test timed out after 30s)"
        if args and result.returncode != 0 and (
            "unrecognized" in result.stderr.lower()
            or "error: argument" in result.stderr.lower()
        ):
            continue
        break

    assert result is not None
    output = (result.stdout or "").strip()
    lines = output.splitlines()[:10]
    snippet = "\n".join(lines) if lines else "(no output)"
    return result.returncode, snippet


# ─── Installed-skills inventory (mimir skills list) ──────────────────


def list_installed(home: Path) -> list[OptionalSkill]:
    """Walk ``<home>/skills/`` and return one entry per skill.

    Same shape as ``list_available`` (both reuse ``_walk_skills_dir``)
    so the CLI can format both listings the same way.
    """
    return _walk_skills_dir(home_skills_dir(home))


# ─── Listing-format helpers ─────────────────────────────────────────


def _truncate_desc(desc: str, budget: int = _DESC_BUDGET) -> str:
    """Truncate ``desc`` to ``budget`` chars with an ellipsis suffix.

    Used by both ``cmd_list`` and ``cmd_list_optional`` so the
    description column has consistent width. Was previously copy-pasted
    in both; centralized per issue #226 nit 4.
    """
    if len(desc) <= budget:
        return desc
    # Reserve 3 chars for the "..." suffix.
    return desc[: budget - 3] + "..."


def _format_skill_row(skill: OptionalSkill, name_width: int) -> str:
    """Format one OptionalSkill as a listing row with fixed-width name
    + fixed-width [poller] flag + description columns.

    Both name AND flag get their own padding so rows with and without
    the flag stay vertically aligned (issue #226 nit 3). Without this,
    "[poller]" appended to the name column shifted the description
    column right on every poller row.
    """
    flag = "[poller]" if skill.has_pollers_json else ""
    return (
        f"  {skill.name:<{name_width}}  "
        f"{flag:<{_FLAG_COL_WIDTH}}  "
        f"{_truncate_desc(skill.description)}"
    )


# ─── CLI wiring (mimir skills install / mimir skills list-optional) ──


def add_argparse_install(parser) -> None:
    """Wire ``mimir skills install <name> [--home PATH] [--force]``."""
    parser.add_argument(
        "name",
        help="Skill bundle name (directory under optional-skills/). "
             "Run `mimir skills list-optional` to see what's available.",
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=None,
        help="Target mimir home (default: $MIMIR_HOME or cwd).",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite an existing skill directory at the destination. "
             "Recursive rm under <home>/skills/<name>/ — any "
             "custom edits there are lost.",
    )
    parser.add_argument(
        "--configure", action="store_true",
        help="Interactively prompt for env vars declared in SKILL.md frontmatter "
             "and write them to <home>/.env. Also runs a smoke test for poller skills.",
    )
    parser.add_argument(
        "--reconfigure", action="store_true",
        help="Like --configure but re-prompts vars already set in .env. Implies --configure.",
    )
    parser.add_argument(
        "--no-smoke-test", action="store_true", dest="no_smoke_test",
        help="With --configure: skip the poller smoke test.",
    )
    parser.set_defaults(skill_install_cmd=cmd_install)


def add_argparse_list_optional(parser) -> None:
    """Wire ``mimir skills list-optional``."""
    parser.set_defaults(skill_install_cmd=cmd_list_optional)


def _resolve_home(home_arg: Path | None) -> Path:
    """Same precedence as the rest of the mimir CLI: --home > $MIMIR_HOME > cwd."""
    if home_arg is not None:
        return home_arg.resolve()
    env = os.environ.get("MIMIR_HOME")
    if env:
        return Path(env).resolve()
    return Path.cwd().resolve()


def cmd_install(args) -> int:
    """``mimir skills install <name>`` entry point."""
    home = _resolve_home(args.home)
    if not home.is_dir():
        print(f"home not a directory: {home}")
        return 2
    try:
        result = install(args.name, home, force=args.force)
    except FileNotFoundError as exc:
        print(str(exc))
        return 2
    except FileExistsError as exc:
        print(str(exc))
        return 3

    verb = "overwrote" if result.overwrote else "installed"
    print(f"{verb} skill {result.name!r} into {result.dest}")

    do_configure = getattr(args, "configure", False) or getattr(args, "reconfigure", False)
    if do_configure:
        env_path = home / ".env"
        required_specs, optional_specs = read_env_specs(result.dest)
        if required_specs or optional_specs:
            written = prompt_and_write_env(
                required_specs,
                optional_specs,
                env_path,
                skill_name=result.name,
                reconfigure=getattr(args, "reconfigure", False),
            )
            result.env_vars_written.extend(written)
            if written:
                print(f"\n  {len(written)} var(s) written to {env_path}")
        else:
            print("  (no env: block in SKILL.md — nothing to configure)")

        if result.pollers_registered_hint and not getattr(args, "no_smoke_test", False):
            print("\n  Running smoke test...")
            exit_code, snippet = run_smoke_test(result.dest, env_path)
            if exit_code == -1:
                print(f"  smoke test: {snippet}")
            elif exit_code == 0:
                print(f"  smoke test: ✓ exited 0")
                if snippet != "(no output)":
                    print(f"  output:\n{snippet}")
            else:
                print(f"  smoke test: ✗ exited {exit_code}")
                if snippet != "(no output)":
                    print(f"  output:\n{snippet}")

    if result.pollers_registered_hint:
        print(
            "\n  this skill ships a pollers.json — to activate, either:\n"
            "    - Restart `mimir run`, OR\n"
            '    - Ask mimir (in any channel it\'s listening on): "please call reload_pollers"'
        )
    elif not do_configure:
        skill_md = result.dest / "SKILL.md"
        if skill_md.is_file():
            print(
                f"  next: read {skill_md.relative_to(home)} for any required "
                f"env vars / external setup (OAuth, API keys, etc.)."
            )
    return 0


def cmd_list_optional(args) -> int:
    """``mimir skills list-optional`` entry point."""
    # Distinguish the wheel-install case ("optional-skills tree doesn't
    # exist") from the source-tree empty-listing case so operators know
    # what to do.
    resolved = _resolve_optional_skills_root()
    if resolved is None:
        print(_PIP_INSTALL_HINT.format(expected=DEFAULT_OPTIONAL_SKILLS_ROOT))
        return 2

    skills = list_available()
    if not skills:
        print(f"no optional skills found under {resolved}")
        return 0
    width = max(len(s.name) for s in skills)
    for s in skills:
        print(_format_skill_row(s, width))
    print(
        f"\ninstall: mimir skills install <name> [--home PATH]\n"
        f"source:  {resolved}"
    )
    return 0


def add_argparse_list(parser) -> None:
    """Wire ``mimir skills list [--home PATH]``."""
    parser.add_argument(
        "--home",
        type=Path,
        default=None,
        help="Mimir home to inspect (default: $MIMIR_HOME or cwd).",
    )
    parser.set_defaults(skill_install_cmd=cmd_list)


def cmd_list(args) -> int:
    """``mimir skills list`` entry point — show installed skills in a home."""
    home = _resolve_home(args.home)
    skills_dir = home_skills_dir(home)
    if not skills_dir.is_dir():
        print(f"no skills installed at {skills_dir} "
              f"(home: {home}). Did you run `mimir setup` here?")
        return 0
    skills = list_installed(home)
    if not skills:
        print(f"no skills found under {skills_dir}")
        return 0
    width = max(len(s.name) for s in skills)
    pollers = [s for s in skills if s.has_pollers_json]
    print(f"installed skills in {home}/skills/  "
          f"(n={len(skills)}, pollers={len(pollers)}):\n")
    for s in skills:
        print(_format_skill_row(s, width))
    print(
        f"\nadd more:    mimir skills install <name> [--home PATH]\n"
        f"available:   mimir skills list-optional"
    )
    return 0


# ─── Drift detection (mimir skills update) ──────────────────────────

#: Paths excluded from drift comparison on both sides (same as
#: the ignore pattern used in ``install()``).
_DRIFT_IGNORE: frozenset[str] = frozenset({"__pycache__", ".pytest_cache"})


def _file_hashes(root: Path) -> dict[str, str]:
    """Return ``relative-path-str → sha256-hex`` for every file under
    *root*, recursively.  Skips any path whose components include a
    name in ``_DRIFT_IGNORE`` (``__pycache__``, ``.pytest_cache``).
    """
    hashes: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        # Drop any path that passes through an ignored directory.
        if any(part in _DRIFT_IGNORE for part in rel.parts):
            continue
        hashes[str(rel)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


@dataclass
class SkillDriftResult:
    """Comparison result for one installed optional skill vs its source.

    Attributes
    ----------
    name:
        The skill directory name (e.g. ``"github-poller"``).
    installed_path:
        Path to the installed copy (``<home>/skills/<name>/``).
    source_path:
        Path to the source copy (``<repo>/optional-skills/<name>/``).
        ``None`` when the skill is orphaned (no matching source entry).
    differs:
        Relative file paths present in **both** installed and source but
        with different content.  Neutral language: the operator may or may
        not have hand-edited these files locally; ``--apply`` will
        overwrite them, so the label avoids implying directionality.
    added:
        Relative file paths present in source but **missing** from the
        installed copy (new files added in source since install).
    extra:
        Relative file paths present in the installed copy but **absent**
        from source (local additions, or source deletions).
    orphaned:
        ``True`` when the source root has no counterpart directory for
        this skill — it was installed from somewhere else, or the source
        tree was restructured.
    """

    name: str
    installed_path: Path
    source_path: Path | None
    differs: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    orphaned: bool = False

    @property
    def is_clean(self) -> bool:
        """True when the installed skill is identical to its source."""
        return not (self.differs or self.added or self.extra or self.orphaned)

    @property
    def has_pollers_json(self) -> bool:
        return (self.installed_path / "pollers.json").is_file()


def detect_skill_drift(
    home: Path,
    optional_skills_root: Path | None = None,
    *,
    name: str | None = None,
) -> list[SkillDriftResult]:
    """Compare installed optional skills against their source counterparts.

    For each installed skill found under ``<home>/skills/``, compares
    file contents against ``<repo>/optional-skills/<name>/`` and returns
    a ``SkillDriftResult`` describing any differences.

    Parameters
    ----------
    home:
        Agent home directory.
    optional_skills_root:
        Source root for optional skills.  Defaults to
        ``DEFAULT_OPTIONAL_SKILLS_ROOT`` (repo-relative).
    name:
        If given, only the named skill is compared.  Raises
        ``FileNotFoundError`` if the skill isn't installed.
    """
    installed_root = home_skills_dir(home)
    src_root = _resolve_optional_skills_root(optional_skills_root)

    if name is not None:
        installed_dir = installed_root / name
        if not installed_dir.is_dir():
            raise FileNotFoundError(
                f"optional skill {name!r} is not installed under {installed_root}."
            )
        installed_dirs = [installed_dir]
    else:
        if not installed_root.is_dir():
            return []
        installed_dirs = sorted(
            d for d in installed_root.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        )

    results: list[SkillDriftResult] = []
    for installed_dir in installed_dirs:
        skill_name = installed_dir.name
        source_dir = (src_root / skill_name) if src_root is not None else None

        if source_dir is None or not source_dir.is_dir():
            results.append(SkillDriftResult(
                name=skill_name,
                installed_path=installed_dir,
                source_path=None,
                orphaned=True,
            ))
            continue

        installed_hashes = _file_hashes(installed_dir)
        source_hashes = _file_hashes(source_dir)

        differs = sorted(
            rel for rel in installed_hashes
            if rel in source_hashes
            and installed_hashes[rel] != source_hashes[rel]
        )
        added = sorted(rel for rel in source_hashes if rel not in installed_hashes)
        extra = sorted(rel for rel in installed_hashes if rel not in source_hashes)

        results.append(SkillDriftResult(
            name=skill_name,
            installed_path=installed_dir,
            source_path=source_dir,
            differs=differs,
            added=added,
            extra=extra,
        ))

    return results


# ─── CLI wiring (mimir skills update) ────────────────────────────────


def _print_drift_report(result: SkillDriftResult) -> None:
    """Print one skill's drift result to stdout."""
    if result.orphaned:
        print(f"{result.name}: orphaned (no source counterpart in optional-skills/)")
        return

    if result.is_clean:
        print(f"{result.name}: up to date")
        return

    total = len(result.differs) + len(result.added) + len(result.extra)
    suffix = f" ({total} file{'s' if total != 1 else ''} differ)"
    print(f"{result.name}:{suffix}")
    for rel in result.differs:
        print(f"  differs from source: {rel}")
    for rel in result.added:
        print(f"  added in source: {rel}")
    for rel in result.extra:
        print(f"  extra in installed: {rel}")
    if result.source_path is not None:
        print(f"  (source: {result.source_path})")


def apply_skill_update(
    result: SkillDriftResult,
    *,
    force: bool = False,
) -> tuple[list[str], list[str], str | None]:
    """Apply pending updates from source to the installed copy of one skill.

    Parameters
    ----------
    result:
        A ``SkillDriftResult`` describing what has drifted.  Orphaned
        skills are skipped (a warning is printed; nothing is written).
    force:
        When *True*, also overwrite files in ``result.extra`` (files
        present only in the installed copy, which may be local edits).
        When *False*, extra files are preserved and a warning is printed.

    Before overwriting any file listed in ``result.differs`` (i.e. a file
    that exists in both the installed copy and source but has different
    content), a backup is written to
    ``.pre-update-backup/<ISO-timestamp>/<rel>`` relative to the installed
    skill directory, and a note is printed.  If the backup itself fails, the
    overwrite is skipped and the file is added to ``failed`` — the safety
    guarantee is absolute: either the backup succeeds and the overwrite is
    recoverable, or the file is left untouched.

    Each ``shutil.copy2`` call is wrapped in ``try/except (OSError,
    IOError)``; per-file failures are logged, the update continues on
    remaining files, and the caller receives the list of failed files so it
    can set the exit code correctly.

    Returns
    -------
    updated_files:
        Relative paths of files that were actually written to disk.
    failed_files:
        Relative paths of files that could not be written (backup failure,
        copy error, or remove error).  Non-empty means the update is partial.
    pollers_hint:
        If a ``pollers.json`` file was among the updated files, a
        human-readable hint string asking the operator to reload pollers.
        ``None`` otherwise.
    """
    if result.orphaned:
        print(
            f"  {result.name}: orphaned (no source counterpart) — skipping; "
            "remove manually if no longer needed."
        )
        return [], [], None

    updated: list[str] = []
    failed: list[str] = []

    assert result.source_path is not None  # guaranteed when not orphaned

    # Backup directory for differs files: .pre-update-backup/<timestamp>/ inside
    # the installed skill directory.  Created lazily on first use.
    backup_root: Path | None = None

    def _ensure_backup_root() -> Path:
        nonlocal backup_root
        if backup_root is None:
            ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup_root = result.installed_path / ".pre-update-backup" / ts
            backup_root.mkdir(parents=True, exist_ok=True)
        return backup_root

    # Overwrite changed files and copy new files from source.
    for rel in result.differs + result.added:
        src_file = result.source_path / rel
        dst_file = result.installed_path / rel

        # For files that differ, write a backup before overwriting so no data
        # is silently lost.  If the backup fails, skip the overwrite entirely
        # and record as failed — never proceed with a destructive write without
        # a functioning safety net.
        if rel in result.differs and dst_file.is_file():
            backup_dir = _ensure_backup_root()
            backup_path = backup_dir / rel
            try:
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dst_file, backup_path)
                print(f"  Note: backed up {rel} → {backup_path} before overwrite")
            except (OSError, IOError) as exc:
                print(
                    f"  error: could not back up {rel} before overwrite: {exc} — skipping"
                )
                failed.append(rel)
                continue

        try:
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst_file)
            updated.append(rel)
        except (OSError, IOError) as exc:
            print(f"  error: failed to copy {rel}: {exc}")
            failed.append(rel)

    # Extra files (local additions or source deletions).
    for rel in result.extra:
        if force:
            extra_file = result.installed_path / rel
            try:
                extra_file.unlink(missing_ok=True)
                updated.append(rel)
            except (OSError, IOError) as exc:
                print(f"  error: failed to remove extra file {rel}: {exc}")
                failed.append(rel)
        else:
            print(
                f"  {result.name}: extra file {rel!r} kept "
                "(local edit — use --force to overwrite)"
            )

    if failed:
        print(
            f"  {result.name}: {len(failed)} file(s) could not be updated: "
            f"{', '.join(failed)}"
        )

    pollers_hint: str | None = None
    if "pollers.json" in updated:
        pollers_hint = (
            f"{result.name}: pollers.json updated — "
            "use the `mcp__mimir__reload_pollers` tool or restart the agent "
            "to register changes"
        )

    return updated, failed, pollers_hint


def add_argparse_update(parser) -> None:
    """Wire ``mimir skills update [<name>|--all] [--apply] [--force] [--home PATH]``."""
    parser.add_argument(
        "name",
        nargs="?",
        default=None,
        help="Skill name to check.  Omit (or use --all) to check all installed optional skills.",
    )
    parser.add_argument(
        "--all",
        dest="all_skills",
        action="store_true",
        default=False,
        help=(
            "Check (or update with --apply) all installed optional skills.  "
            "Equivalent to omitting the skill name argument, but explicit.  "
            "Cannot be combined with a positional skill name."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help=(
            "Overwrite installed files that differ from source and copy new "
            "files added in source.  Without this flag the command is "
            "read-only (dry-run)."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help=(
            "With --apply: also remove extra files that exist only in the "
            "installed copy (possible local edits).  Has no effect without "
            "--apply."
        ),
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=None,
        help="Mimir home (default: $MIMIR_HOME or cwd).",
    )
    parser.add_argument(
        "--optional-skills-root",
        type=Path,
        default=None,
        dest="optional_skills_root",
        help="Source root for optional skills (default: <repo>/optional-skills/). "
             "Only needed when running from outside the mimir source tree.",
    )
    parser.set_defaults(skill_install_cmd=cmd_update_skills)


def cmd_update_skills(args) -> int:
    """``mimir skills update`` entry point.

    Without ``--apply``: compares installed optional skills against their
    source counterparts and prints a per-skill drift report.  Exits 0 when
    all skills are up-to-date; exits 1 when any drift is found.

    With ``--apply``: overwrites installed files that differ from source
    and copies new files added in source.  Extra files (local additions)
    are preserved unless ``--force`` is also given.  Exits 0 on success;
    exits 1 when any skill could not be fully updated (e.g. skipped extras
    without --force, or per-file copy/backup failures); exits 2 on usage
    errors.

    ``--all``: explicit "check every installed skill" flag.  Equivalent to
    omitting the positional name argument but makes the intent unambiguous.
    Mutually exclusive with a positional skill name.
    """
    home = _resolve_home(getattr(args, "home", None))
    if not home.is_dir():
        print(f"home not a directory: {home}")
        return 2

    skill_name: str | None = getattr(args, "name", None)
    all_skills: bool = getattr(args, "all_skills", False)

    if all_skills and skill_name is not None:
        print("error: --all and a skill name are mutually exclusive.")
        return 2

    # --all is equivalent to omitting the name (name=None scans all skills).
    resolved_name: str | None = None if all_skills else skill_name

    src_root: Path | None = getattr(args, "optional_skills_root", None)
    do_apply: bool = getattr(args, "apply", False)
    do_force: bool = getattr(args, "force", False)

    try:
        results = detect_skill_drift(home, src_root, name=resolved_name)
    except FileNotFoundError as exc:
        print(str(exc))
        return 2

    if not results:
        print("no optional skills installed.")
        return 0

    if not do_apply:
        # Dry-run: print drift report and exit.
        for r in results:
            _print_drift_report(r)

        drift_found = any(not r.is_clean for r in results)
        if drift_found:
            dirty = [r.name for r in results if not r.is_clean]
            print(
                f"\n{len(dirty)} skill{'s' if len(dirty) != 1 else ''} out of date: "
                f"{', '.join(dirty)}"
            )
        return 1 if drift_found else 0

    # Apply mode: update each skill, collect pollers hints.
    any_skipped_extras = False
    any_failures = False
    pollers_hints: list[str] = []

    for r in results:
        if r.is_clean:
            print(f"{r.name}: up to date")
            continue

        updated, failed, hint = apply_skill_update(r, force=do_force)

        if hint:
            pollers_hints.append(hint)

        if failed:
            any_failures = True

        if updated:
            print(
                f"{r.name}: updated {len(updated)} "
                f"file{'s' if len(updated) != 1 else ''} "
                f"({', '.join(updated)})"
            )
        elif not r.orphaned:
            # No files written — only had extra files but --force not set.
            any_skipped_extras = True

        if not do_force and r.extra and not r.orphaned:
            any_skipped_extras = True

    if pollers_hints:
        print()
        for hint in pollers_hints:
            print(hint)

    return 1 if (any_failures or any_skipped_extras) else 0


# ─── Configure (mimir skills configure) ─────────────────────────────


def find_skill_path(home: Path, name: str) -> Path | None:
    """Return the path to an installed skill's directory.

    Checks operator-installed skills (``<home>/skills/<name>/``) first,
    then bundled built-ins (``<home>/.mimir_builtin_skills/<name>/``).
    Returns ``None`` when the skill isn't found in either location.
    """
    from mimir.skill_defs import home_builtin_skills_dir, home_skills_dir

    for root in (home_skills_dir(home), home_builtin_skills_dir(home)):
        candidate = root / name
        if candidate.is_dir() and (candidate / "SKILL.md").is_file():
            return candidate
    return None


def walk_configurable_skills(home: Path) -> list[tuple[str, Path]]:
    """Return ``(name, path)`` pairs for every skill that has an
    ``env:`` block in its ``SKILL.md`` (i.e., has env vars to configure).

    Operator-installed skills shadow built-ins when names collide.  Only
    skills with at least one required *or* optional env-var entry are
    included — skills with no ``env:`` block are silently skipped.
    """
    from mimir.skill_defs import installed_skill_names

    result: list[tuple[str, Path]] = []
    for name in installed_skill_names(home):
        path = find_skill_path(home, name)
        if path is None:
            continue
        required, optional = read_env_specs(path)
        if required or optional:
            result.append((name, path))
    return result


def _configure_one(
    name: str,
    path: Path,
    home: Path,
    *,
    reconfigure: bool,
    no_smoke_test: bool,
) -> int:
    """Interactive env-var configuration for a single skill.

    Prompts for required + optional vars declared in ``SKILL.md``,
    writes them to ``<home>/.env``, and (unless ``no_smoke_test``)
    runs the poller smoke test when the skill ships a ``pollers.json``.

    Returns 0 on success; 0 even when no vars were written (the user
    skipped prompts) so callers can proceed without special-casing.
    """
    env_path = home / ".env"
    required_specs, optional_specs = read_env_specs(path)

    if not required_specs and not optional_specs:
        print(f"{name}: no env: block in SKILL.md — nothing to configure")
        return 0

    written = prompt_and_write_env(
        required_specs,
        optional_specs,
        env_path,
        skill_name=name,
        reconfigure=reconfigure,
    )
    if written:
        print(f"\n  {len(written)} var(s) written to {env_path}")

    # Smoke test — only meaningful for poller skills.
    if not no_smoke_test and (path / "pollers.json").is_file():
        print("\n  Running smoke test...")
        exit_code, snippet = run_smoke_test(path, env_path)
        if exit_code == -1:
            print(f"  smoke test: {snippet}")
        elif exit_code == 0:
            print("  smoke test: ✓ exited 0")
            if snippet != "(no output)":
                print(f"  output:\n{snippet}")
        else:
            print(f"  smoke test: ✗ exited {exit_code}")
            if snippet != "(no output)":
                print(f"  output:\n{snippet}")

    # Reload hint — shown whether or not env vars were written.
    if (path / "pollers.json").is_file():
        print(
            "\n  this skill ships a pollers.json — to activate changes:\n"
            "    - Restart `mimir run`, OR\n"
            '    - Ask mimir: "please call reload_pollers"'
        )

    return 0


def add_argparse_configure(parser) -> None:
    """Wire ``mimir skills configure [<name>] [--all] [--home PATH]``."""
    name_group = parser.add_mutually_exclusive_group(required=True)
    name_group.add_argument(
        "name",
        nargs="?",
        default=None,
        help="Skill name to configure (e.g. weather, github-poller). "
             "Run `mimir skills list` to see installed skills.",
    )
    name_group.add_argument(
        "--all",
        dest="all_skills",
        action="store_true",
        default=False,
        help="Configure every skill in the home that has env vars declared "
             "in its SKILL.md. Skips skills with no env: block.",
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=None,
        help="Mimir home (default: $MIMIR_HOME or cwd).",
    )
    parser.add_argument(
        "--reconfigure",
        action="store_true",
        help="Re-prompt even for vars already set in .env. "
             "Without this flag, already-set vars are skipped.",
    )
    parser.add_argument(
        "--no-smoke-test",
        action="store_true",
        dest="no_smoke_test",
        help="Skip the poller smoke test (useful in non-interactive / CI contexts).",
    )
    parser.set_defaults(skill_install_cmd=cmd_configure)


def cmd_configure(args) -> int:
    """``mimir skills configure`` entry point.

    Interactive env-var setup for an installed skill (including bundled
    built-ins like ``weather`` and ``ntfy``).  Reads the skill's
    ``SKILL.md`` ``env:`` block, prompts for each var, and writes the
    results to ``<home>/.env``.  Use ``--reconfigure`` to re-prompt vars
    that are already set.
    """
    home = _resolve_home(getattr(args, "home", None))
    if not home.is_dir():
        print(f"home not a directory: {home}")
        return 2

    reconfigure: bool = getattr(args, "reconfigure", False)
    no_smoke_test: bool = getattr(args, "no_smoke_test", False)

    if getattr(args, "all_skills", False):
        skills = walk_configurable_skills(home)
        if not skills:
            print("no configurable skills found (none have env: blocks in their SKILL.md)")
            return 0
        for name, path in skills:
            print(f"\n{'─' * 40}")
            print(f"Configuring {name!r} …")
            _configure_one(name, path, home, reconfigure=reconfigure, no_smoke_test=no_smoke_test)
        return 0

    name: str | None = getattr(args, "name", None)
    if not name:
        # argparse enforces the mutually_exclusive_group(required=True), but
        # guard defensively for programmatic invocations.
        print("error: specify a skill name or pass --all")
        return 2

    path = find_skill_path(home, name)
    if path is None:
        from mimir.skill_defs import home_builtin_skills_dir
        builtin_root = home_builtin_skills_dir(home)
        if not builtin_root.is_dir() or not any(builtin_root.iterdir()):
            print(f"skill not found: {name!r}")
            print(f"  tip: run `mimir setup --home {home}` first to seed bundled skills")
        else:
            print(f"skill not found: {name!r}")
            print("  tip: run `mimir skills list` to see installed skills")
        return 2

    return _configure_one(name, path, home, reconfigure=reconfigure, no_smoke_test=no_smoke_test)
