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

import hashlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from mimir.skill_defs import home_skills_dir
from mimir.skill_md import parse_env_block, parse_frontmatter

# Source root for optional-skills, relative to this file.
#: ``mimir/skill_install.py`` lives at ``<repo>/mimir/skill_install.py``;
#: optional-skills lives at ``<repo>/optional-skills/``. One level up
#: from this file gives the repo root.
#:
#: Caveat: this resolves correctly only from a source-tree install (uv
#: editable install, git clone). A pip-installed mimir wheel doesn't
#: package the ``optional-skills/`` tree at all — callers that hit this
#: path get a clear error message via ``_resolve_optional_skills_root``
#: rather than the silent "no optional skills available" footgun.
DEFAULT_OPTIONAL_SKILLS_ROOT = Path(__file__).parent.parent / "optional-skills"

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
    "This usually means mimir was installed from a wheel (pip / pipx) — "
    "optional skills aren't packaged for wheel distribution. To use "
    "`mimir skills install` / `list-optional`, run from a source-tree "
    "install:\n"
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


def install(
    name: str,
    home: Path,
    *,
    force: bool = False,
    optional_skills_root: Path | None = None,
) -> InstallResult:
    """Copy an opt-in skill into the agent home.

    Raises:
        FileNotFoundError: source skill doesn't exist (either the
            optional-skills root is missing entirely, or the named skill
            isn't under it).
        FileExistsError: destination exists and ``force`` is False.
    """
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

    Tries ``python3 poller.py --once`` first; if the poller doesn't accept
    ``--once``, retries without it. Returns ``(-1, 'no poller.py found')``
    when the skill has no ``poller.py``.
    """
    poller = dest / "poller.py"
    if not poller.is_file():
        return -1, "no poller.py found"

    env = os.environ.copy()
    if env_path and env_path.is_file():
        for k, v in _read_env_file(env_path).items():
            env.setdefault(k, v)

    result: subprocess.CompletedProcess[str] | None = None
    for args in (["--once"], []):
        try:
            result = subprocess.run(
                ["python3", str(poller), *args],
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
            "\n  this skill ships a pollers.json — once mimir is running, "
            "call the `reload_pollers` tool (or restart `mimir run`) so "
            "the poller registers."
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
    modified:
        Relative file paths present in **both** but with different
        content (source is newer/different).
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
    modified: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    orphaned: bool = False

    @property
    def is_clean(self) -> bool:
        """True when the installed skill is identical to its source."""
        return not (self.modified or self.added or self.extra or self.orphaned)

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

        modified = sorted(
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
            modified=modified,
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

    total = len(result.modified) + len(result.added) + len(result.extra)
    suffix = f" ({total} file{'s' if total != 1 else ''} differ)"
    print(f"{result.name}:{suffix}")
    for rel in result.modified:
        print(f"  modified: {rel}")
    for rel in result.added:
        print(f"  added in source: {rel}")
    for rel in result.extra:
        print(f"  extra in installed: {rel}")
    if result.source_path is not None:
        print(f"  (source: {result.source_path})")


def add_argparse_update(parser) -> None:
    """Wire ``mimir skills update [<name>] [--home PATH]``."""
    parser.add_argument(
        "name",
        nargs="?",
        default=None,
        help="Skill name to check.  Omit to check all installed optional skills.",
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

    Compares installed optional skills against their source counterparts
    and prints a per-skill drift report.  Exits 0 when all skills are
    up-to-date; exits 1 when any drift is found (useful for CI).
    Exits 2 on usage errors (home not a directory, named skill not found).
    """
    home = _resolve_home(getattr(args, "home", None))
    if not home.is_dir():
        print(f"home not a directory: {home}")
        return 2

    src_root: Path | None = getattr(args, "optional_skills_root", None)

    try:
        results = detect_skill_drift(home, src_root, name=getattr(args, "name", None))
    except FileNotFoundError as exc:
        print(str(exc))
        return 2

    if not results:
        print("no optional skills installed.")
        return 0

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
