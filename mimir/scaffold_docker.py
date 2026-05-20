"""Scaffold container-deployment files for an agent home.

Generates ``Dockerfile``, ``compose.yml``, ``compose.env`` (operator-edited
secrets file), and ``start.sh`` into a mimir home so it can be deployed
in a container the same way mimirbot is. Inspects ``<home>/.claude/skills/``
to pick up per-skill OS-level deps (a skill that needs a system tool
ships a ``dockerfile.fragment`` next to its ``SKILL.md``) and per-skill
env-var requirements (``pollers.json`` ``pass_env``).

**Idempotency contract:**

* ``Dockerfile``: regenerated fully each run. Skill fragments are
  collected from disk and stitched into a sentinel-marked block.
  Adding or removing a skill + re-running picks up the change.
* ``compose.yml`` and ``start.sh``: regenerated fully each run.
* ``compose.env``: **merge mode** — existing operator values are
  preserved; only missing required keys are appended as commented
  placeholders. Re-running after installing a poller adds its new
  env vars without touching the operator's existing secrets.

The base template draws from mimirbot's actual Dockerfile —
python:3.11-slim + git/gh/uv/Claude Code CLI/Node + a ``mimir``
non-root user — generalized so the container name is parametric.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path


# ── Data shapes ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class Fragment:
    skill_name: str
    content: str  # raw Dockerfile lines (no surrounding blank lines)


@dataclass
class ScaffoldResult:
    files_written: list[str] = field(default_factory=list)
    files_skipped: list[str] = field(default_factory=list)
    env_vars_added: list[str] = field(default_factory=list)
    skills_with_fragments: list[str] = field(default_factory=list)


# ── Collection helpers ──────────────────────────────────────────────


#: Bundled skill roots, used as fallback when a skill present in the
#: home doesn't have its own ``dockerfile.fragment`` (because the home
#: was seeded by an earlier ``mimir setup`` that predated the fragment
#: file landing in the bundle). ``mimir.cli.seed_skills`` is
#: won't-overwrite idempotent — once a skill dir exists in a home, new
#: files added to the bundled source won't be back-filled.
_BUNDLED_SKILL_ROOTS = (
    Path(__file__).parent / "skills",
    Path(__file__).parent.parent / "optional-skills",
)


def _read_fragment(skill_name: str, *roots: Path) -> str | None:
    """Try home's installed skill first, then bundled roots. Returns
    the fragment's stripped contents or None if nothing found."""
    for root in roots:
        frag = root / skill_name / "dockerfile.fragment"
        if frag.is_file():
            content = frag.read_text().strip()
            if content:
                return content
    return None


def collect_fragments(home: Path) -> list[Fragment]:
    """For each skill present in ``<home>/.claude/skills/``, look up its
    ``dockerfile.fragment`` — preferring the installed copy in the home,
    falling back to the bundled source (``mimir/skills/<name>/`` or
    ``optional-skills/<name>/``). Ordered alphabetically by skill name
    for stable Dockerfile output.

    The fallback handles the case where a home was seeded before the
    fragment file existed in the bundle: ``seed_skills`` won't refresh
    an existing skill dir, so the operator's home lacks the new file
    even though they have the skill. The scaffolder paints over that
    gap.
    """
    skills_root = home / ".claude" / "skills"
    if not skills_root.is_dir():
        return []
    out: list[Fragment] = []
    for skill_dir in sorted(skills_root.iterdir()):
        if not skill_dir.is_dir() or skill_dir.name.startswith("."):
            continue
        content = _read_fragment(
            skill_dir.name,
            skills_root,  # check home first (operator may have edited)
            *_BUNDLED_SKILL_ROOTS,  # then bundled defaults / optional
        )
        if content is None:
            continue
        out.append(Fragment(skill_name=skill_dir.name, content=content))
    return out


def collect_required_env_vars(home: Path) -> list[str]:
    """Walk ``<home>/.claude/skills/*/pollers.json`` and collect every
    env var listed in ``pass_env`` across all poller skills. De-duped,
    sorted. Plus baseline mimir vars (MIMIR_API_KEY, etc.).
    """
    baseline = [
        "MIMIR_API_KEY",           # auth gate for non-shell HTTP routes
        "ANTHROPIC_API_KEY",       # if using anthropic provider (not claude_code)
        "CLAUDE_CODE_OAUTH_TOKEN", # if using claude_code provider (Max plan)
        "VOYAGE_API_KEY",          # saga embeddings (voyage-4-lite)
        "OPENAI_API_KEY",          # judge / fallback embed / openai_compat LLM
        "GITHUB_TOKEN",            # gh auth for cloning + pushing
        "GH_USER_NAME",            # git config user.name
        "GH_USER_EMAIL",           # git config user.email
        "MIMIR_GIT_URL",           # mimir source clone URL — change for forks
        "MIMIR_DEFAULT_BRANCH",    # branch start.sh clones (default: main)
    ]
    seen = set(baseline)
    extra: list[str] = []
    skills_root = home / ".claude" / "skills"
    if skills_root.is_dir():
        for skill_dir in sorted(skills_root.iterdir()):
            if not skill_dir.is_dir() or skill_dir.name.startswith("."):
                continue
            pollers_json = skill_dir / "pollers.json"
            if not pollers_json.is_file():
                continue
            try:
                spec = json.loads(pollers_json.read_text())
            except json.JSONDecodeError:
                continue
            # Defensive: a pollers.json that's valid JSON but has the
            # wrong shape (``"pollers": "oops"`` instead of an array)
            # would crash on the next ``.get(...)`` chain. Skip it.
            if not isinstance(spec, dict):
                continue
            pollers_list = spec.get("pollers", [])
            if not isinstance(pollers_list, list):
                continue
            for p in pollers_list:
                if not isinstance(p, dict):
                    continue
                pass_env = p.get("pass_env", [])
                if not isinstance(pass_env, list):
                    continue
                for var in pass_env:
                    if isinstance(var, str) and var not in seen:
                        seen.add(var)
                        extra.append(var)
    return baseline + extra


# ── Renderers ───────────────────────────────────────────────────────


# Sentinel that separates the fragments-block placeholder from the
# rest of the template. Using a unique HTML-comment-shaped token (not
# ``{FRAGMENTS}``) so that a stray ``{FRAGMENTS}`` inside a skill's
# dockerfile.fragment couldn't ever corrupt the substitution.
_FRAGMENTS_SENTINEL = "<!-- mimir-scaffold-docker:FRAGMENTS -->"

_DOCKERFILE_BASE = """\
# Generated by `mimir scaffold-docker`. THIS FILE IS REGENERATED IN
# FULL on every run — do NOT edit it in place; your changes WILL be
# lost the next time `mimir scaffold-docker` runs.
#
# To customize the build for this deployment, add a separate layer
# (e.g. ``Dockerfile.custom``) and reference it from compose.yml, or
# author a per-skill ``dockerfile.fragment`` under
# ``<home>/.claude/skills/<name>/`` — those get stitched into the
# sentinel-marked block below on each regeneration.
#
# Build: docker compose build --build-arg USER_UID=$(id -u)
# Run:   docker compose up -d

FROM python:3.11-slim

# Base system tooling — same set mimirbot has been running with since
# 2026-05: git for the source clone + agent dev loop, gh for PR
# creation, build-essential for any C extensions, ca-certificates for
# HTTPS clones, poppler-utils for Claude Code's PDF Read, Node 20
# (claude-agent-sdk + mermaid-cli are npm packages).
RUN apt-get update \\
 && apt-get install -y --no-install-recommends \\
        git \\
        curl \\
        ca-certificates \\
        build-essential \\
        gnupg \\
        tmux \\
        poppler-utils \\
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \\
        | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \\
 && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \\
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \\
        > /etc/apt/sources.list.d/github-cli.list \\
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \\
 && apt-get install -y --no-install-recommends gh nodejs \\
 && rm -rf /var/lib/apt/lists/*

# Claude Code CLI (claude-agent-sdk shells out to this binary) +
# mermaid CLI (used by mermaid-diagrams skill).
RUN npm install -g @anthropic-ai/claude-code @mermaid-js/mermaid-cli

# uv handles dep resolution + virtualenv inside the workspace clone.
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \\
 && mv /root/.local/bin/uv /usr/local/bin/uv \\
 && mv /root/.local/bin/uvx /usr/local/bin/uvx

# ===== BEGIN mimir-scaffold-docker: skill fragments =====
# Auto-generated from <home>/.claude/skills/*/dockerfile.fragment.
# Each skill that ships a fragment contributes one block below.
# Re-run `mimir scaffold-docker` to refresh after installing /
# uninstalling skills.
""" + _FRAGMENTS_SENTINEL + """
# ===== END mimir-scaffold-docker: skill fragments =====

# Non-root user. Claude Code refuses --dangerously-skip-permissions
# (which mimir's permission_mode="bypassPermissions" maps to) when
# the process is uid 0. Match USER_UID to your host UID at build time:
#   docker compose build --build-arg USER_UID=$(id -u)
ARG USER_UID=1000
ARG USER_GID=1000
RUN groupadd --gid "${USER_GID}" mimir \\
 && useradd --uid "${USER_UID}" --gid "${USER_GID}" --create-home --shell /bin/bash mimir \\
 && mkdir -p /workspace /mimir-home \\
 && chown -R mimir:mimir /workspace /mimir-home

WORKDIR /workspace

# ENV PATH uses Docker's standard variable-substitution grammar
# (``${VAR}``) so the base image's PATH propagates. A double-braced
# form like ``$ + { + {PATH} + } + }`` is NOT valid Docker syntax;
# an earlier draft of this template shipped that token literally and
# the resulting container had a broken PATH. The regression test in
# tests/test_scaffold_docker.py asserts this stays clean.
ENV MIMIR_HOME=/mimir-home \\
    UV_LINK_MODE=copy \\
    UV_CACHE_DIR=/workspace/.uv-cache \\
    PATH=/usr/local/bin:${PATH}

EXPOSE 8080

COPY start.sh /usr/local/bin/start.sh
RUN chmod +x /usr/local/bin/start.sh

USER mimir

ENTRYPOINT ["/usr/local/bin/start.sh"]
"""


def render_dockerfile(fragments: list[Fragment]) -> str:
    """Stitch fragments into the base template via sentinel split, so
    a fragment that happens to contain the literal sentinel-string
    can't corrupt the substitution (the split-based insert leaves the
    sentinel unique to the template's own copy)."""
    if not fragments:
        body = "# (no skills installed yet ship a dockerfile.fragment)"
    else:
        parts = []
        for f in fragments:
            parts.append(f"# --- {f.skill_name} ---\n{f.content.strip()}")
        body = "\n\n".join(parts)
    # Use split(..., 1) + join so even a malformed template (sentinel
    # appearing twice for some reason) doesn't multi-paste the body.
    head, sep, tail = _DOCKERFILE_BASE.partition(_FRAGMENTS_SENTINEL)
    if not sep:
        raise RuntimeError(
            "scaffold_docker: _DOCKERFILE_BASE is missing the FRAGMENTS "
            "sentinel — template internal invariant violation."
        )
    return head + body + tail


_COMPOSE_YML_TEMPLATE = """\
# Generated by `mimir scaffold-docker`. THIS FILE IS REGENERATED IN
# FULL on every run — your edits WILL be lost. For per-deployment
# customization (extra services, additional volumes, alternative
# networks), use a docker-compose override file:
# https://docs.docker.com/compose/extends/
#
# Usage:
#   cp compose.env.example compose.env  &&  edit compose.env
#   docker compose up -d --build
#   docker compose logs -f
#
# Iterate (picks up code in /workspace/<repo> without nuking branches):
#   docker compose restart

services:
  {SERVICE_NAME}:
    build: .
    container_name: {SERVICE_NAME}
    restart: unless-stopped
    env_file:
      - compose.env
    environment:
      MIMIR_HOME: /mimir-home
      MIMIR_WEB_PORT: 8080
    ports:
      # 127.0.0.1 binding — defense in depth alongside the auth
      # middleware. To expose on LAN, change to "0.0.0.0:{WEB_PORT}:8080".
      - "127.0.0.1:{WEB_PORT}:8080"
    volumes:
      # Persistent agent state — saga.db, logs, identities.yaml, etc.
      # The current dir IS /mimir-home. .gitignore controls what the
      # auto-push (if enabled) tracks.
      - .:/mimir-home

      # Mimir source clone — named volume, NOT a host bind mount.
      # start.sh handles the first-run clone + uv sync.
      - workspace:/workspace

volumes:
  workspace:
"""


def render_compose_yml(*, service_name: str, web_port: int) -> str:
    return (
        _COMPOSE_YML_TEMPLATE
        .replace("{SERVICE_NAME}", service_name)
        .replace("{WEB_PORT}", str(web_port))
    )


_START_SH_TEMPLATE = """\
#!/usr/bin/env bash
# Generated by `mimir scaffold-docker`. THIS FILE IS REGENERATED IN
# FULL on every run — do NOT edit it in place; your changes WILL be
# lost. For per-deployment behavior, set the relevant env vars in
# compose.env (MIMIR_GIT_URL, MIMIR_DEFAULT_BRANCH, GH_USER_NAME,
# GH_USER_EMAIL, etc.). For deeper customization, replace this
# entrypoint with your own COPY in a Dockerfile override layer.
#
#   1. First run: clone the runtime source (derived from MIMIR_GIT_URL)
#      into /workspace/<repo-name>. Subsequent runs leave the worktree
#      alone — the agent owns its branches + uncommitted state.
#   2. git + gh auth from GITHUB_TOKEN (clone of private upstreams).
#   3. uv sync (idempotent).
#   4. mimir setup --home /mimir-home (idempotent; only writes missing
#      files).
#   5. exec mimir run.
set -euo pipefail

# MIMIR_GIT_URL — clone source for the mimir runtime. Override in
# compose.env if you're running a Muninn-like fork or pinning to a
# private fork; defaults to upstream mimir's main branch.
REPO_URL="${MIMIR_GIT_URL:-https://github.com/jasoncarreira/mimir.git}"
# Derive the local clone dir from the URL so non-mimir forks (Muninn,
# etc.) land at /workspace/<their-repo-name> instead of being mis-named
# /workspace/mimir.
REPO_NAME="$(basename "${REPO_URL}" .git)"
REPO_DIR="/workspace/${REPO_NAME}"
DEFAULT_BRANCH="${MIMIR_DEFAULT_BRANCH:-main}"

# ─── git + gh auth ─────────────────────────────────────────────────
git config --global user.name  "${GH_USER_NAME:-mimir-agent}"
git config --global user.email "${GH_USER_EMAIL:-mimir-agent@local}"
git config --global init.defaultBranch main

if [ -n "${GITHUB_TOKEN:-}" ]; then
    if ! gh auth status >/dev/null 2>&1; then
        echo "${GITHUB_TOKEN}" | gh auth login --with-token
    fi
    gh auth setup-git >/dev/null 2>&1 || true
else
    echo "[start.sh] WARNING: GITHUB_TOKEN unset — clone of private repo will fail"
fi

# ─── Source clone ──────────────────────────────────────────────────
if [ ! -d "${REPO_DIR}/.git" ]; then
    echo "[start.sh] cloning ${REPO_URL} into ${REPO_DIR} (first-run)"
    git clone --branch "${DEFAULT_BRANCH}" "${REPO_URL}" "${REPO_DIR}"
else
    echo "[start.sh] ${REPO_DIR} present — leaving worktree intact"
fi

cd "${REPO_DIR}"
git config --global --add safe.directory "${REPO_DIR}"

# ─── deps sync ─────────────────────────────────────────────────────
# Extras templated from ``mimir scaffold-docker --uv-extras`` so the
# right pyproject.toml ``[project.optional-dependencies]`` get
# resolved at boot. Override per-deployment if your bridges need
# different extras (slack, etc.).
UV_EXTRAS="{UV_EXTRAS}"
echo "[start.sh] uv sync (extras: ${UV_EXTRAS:-(none)})"
uv sync $UV_EXTRAS

# ─── home seed (idempotent — only writes missing files) ────────────
mkdir -p "${MIMIR_HOME}"
echo "[start.sh] mimir setup (idempotent)"
uv run mimir setup --home "${MIMIR_HOME}" || {
    echo "[start.sh] WARNING: mimir setup non-zero — home may be partial" >&2
}

# ─── run ───────────────────────────────────────────────────────────
echo "[start.sh] starting mimir run (home=${MIMIR_HOME}, port=${MIMIR_WEB_PORT:-8080})"
exec uv run mimir run --home "${MIMIR_HOME}"
"""


def render_start_sh(*, uv_extras: list[str] | None = None) -> str:
    """Render start.sh with the given uv extras flags.

    ``uv_extras`` is a list of extra-names (e.g. ``["discord", "claude-code"]``);
    the renderer expands them to the ``--extra <name>`` flags ``uv sync``
    expects. Default is empty (no extras) — caller picks what bridges they
    need.

    **Maintenance constraint** — the template uses two superimposed
    substitution layers: this Python ``.replace()`` at scaffold time
    *and* bash variable expansion at container boot. When adding a new
    Python placeholder (e.g. ``{NEW_VAR}``), use **bare ``$NEW_VAR``,
    NOT ``${NEW_VAR}``** in any shell reference to the same value
    elsewhere in the template — otherwise ``.replace("{NEW_VAR}", "")``
    matches the substring inside ``${NEW_VAR}`` and leaves a stray
    ``$`` that crashes the container at boot. See the regression
    test ``test_render_start_sh_uv_sync_line_is_valid_shell_with_no_extras``
    for the failure shape this constraint prevents.
    """
    extras = uv_extras or []
    flags = " ".join(f"--extra {e}" for e in extras)
    return _START_SH_TEMPLATE.replace("{UV_EXTRAS}", flags)


# ── compose.env idempotent merge ───────────────────────────────────


_ENV_KEY_RE = re.compile(r"^\s*([A-Z_][A-Z0-9_]*)\s*=")
#: Match a commented placeholder like ``# KEY=`` so the idempotency
#: check recognizes "we already templated this key for the operator"
#: as "don't re-append." Without this, re-running scaffold-docker on a
#: freshly-generated compose.env (where every key is still commented)
#: would re-append the same placeholders every time.
_COMMENTED_ENV_KEY_RE = re.compile(r"^\s*#\s*([A-Z_][A-Z0-9_]*)\s*=")


def existing_env_keys(text: str) -> set[str]:
    """Parse a compose.env file and return the set of KEYs the file
    already references, whether as a live ``KEY=value`` setting or as
    a ``# KEY=`` placeholder comment we (or a previous scaffold run)
    emitted. Both forms count as "the file knows about this key" —
    re-running scaffold-docker must NOT append a third copy.
    """
    keys: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        m = _ENV_KEY_RE.match(stripped)
        if m:
            keys.add(m.group(1))
            continue
        m = _COMMENTED_ENV_KEY_RE.match(stripped)
        if m:
            keys.add(m.group(1))
    return keys


_COMPOSE_ENV_HEADER = """\
# Generated by `mimir scaffold-docker`. Operator-edited file — fill in
# values for the keys below. Re-running scaffold-docker preserves your
# values; new keys (added by newly-installed pollers) get appended as
# commented placeholders.
#
# NEVER commit this file to a public repo. .gitignore should exclude
# compose.env at the repo root.

"""


def render_compose_env(existing_text: str | None, required_keys: list[str]) -> tuple[str, list[str]]:
    """Idempotent merge of required env vars into compose.env.

    Returns ``(new_text, newly_added_keys)``. If ``existing_text`` is
    None or empty, writes a fresh header + every required key as a
    commented placeholder. If keys are already present (uncommented),
    they're left alone. Missing keys are appended in a separate block
    at the end so operators can find what's new.
    """
    existing_text = existing_text or ""
    have = existing_env_keys(existing_text)
    missing = [k for k in required_keys if k not in have]

    if not existing_text.strip():
        body = _COMPOSE_ENV_HEADER
        for k in required_keys:
            body += f"# {k}=\n"
        return body, list(required_keys)

    if not missing:
        return existing_text, []

    # Append a marker block for the new keys.
    appendix = "\n\n# ── Added by `mimir scaffold-docker` ──\n"
    appendix += "# New env-var placeholders (from newly-installed pollers /\n"
    appendix += "# updated requirements). Fill in values and remove the leading\n"
    appendix += "# `#` to enable, or delete the line if not needed.\n"
    for k in missing:
        appendix += f"# {k}=\n"

    return existing_text.rstrip() + appendix, missing


# ── Orchestrator ───────────────────────────────────────────────────


def _ensure_compose_env_gitignored(home: Path) -> bool:
    """Append a never-track block for compose.env to the home's
    .gitignore if it's not already covered by an explicit entry.

    The default mimir-home .gitignore is allowlist-style ('* then
    !path/**'), so compose.env is implicitly blocked — but if an
    operator switches to a blocklist, or adds a broad allowlist like
    '!*.env', it could end up tracked. Adding an explicit deny entry
    is cheap belt-and-suspenders.

    Returns True if the file was modified.
    """
    gi = home / ".gitignore"
    sentinel = "# mimir-scaffold-docker: NEVER track compose.env (contains secrets)"
    if gi.is_file():
        existing = gi.read_text()
        if sentinel in existing or "\ncompose.env\n" in existing or existing.startswith("compose.env\n"):
            return False
        new = existing.rstrip() + f"\n\n{sentinel}\ncompose.env\n"
        gi.write_text(new)
    else:
        gi.write_text(f"{sentinel}\ncompose.env\n")
    return True


def _sanitize_service_name(raw: str) -> str:
    """Replace any char outside [A-Za-z0-9_-] with '-'.

    Used for both auto-derived (home dir name) and explicit
    (``--service-name``) inputs so compose never sees an invalid
    service / container name.
    """
    return re.sub(r"[^a-zA-Z0-9_-]", "-", raw)


def scaffold(
    home: Path,
    *,
    service_name: str | None = None,
    web_port: int = 8090,
    uv_extras: list[str] | None = None,
) -> ScaffoldResult:
    """Generate / refresh Docker scaffolding for an agent home.

    Idempotent: re-running picks up new skill fragments / env vars
    without clobbering operator-edited values in compose.env.

    ``uv_extras`` are passed through to ``start.sh`` so ``uv sync``
    resolves the right ``[project.optional-dependencies]`` extras at
    boot (e.g. ``["discord", "claude-code"]`` for a Discord + Max-plan
    deployment). Default empty.
    """
    home = home.resolve()
    if not home.is_dir():
        raise FileNotFoundError(f"home not a directory: {home}")

    if service_name is None:
        # Use the agent's home dir name (sanitized) as the container
        # name. Keeps multiple agents on one host from colliding.
        service_name = _sanitize_service_name(home.name or "mimir-agent")
    else:
        # An explicit --service-name still goes through the sanitizer
        # so an operator typo (spaces, slashes) can't produce an
        # invalid compose service name. Compose silently accepts some
        # of these and then errors deep in the container runtime.
        sanitized = _sanitize_service_name(service_name)
        if sanitized != service_name:
            print(
                f"[scaffold-docker] --service-name {service_name!r} "
                f"contained chars outside [A-Za-z0-9_-]; using "
                f"{sanitized!r} instead."
            )
            service_name = sanitized

    result = ScaffoldResult()
    fragments = collect_fragments(home)
    required_keys = collect_required_env_vars(home)
    result.skills_with_fragments = [f.skill_name for f in fragments]

    # Helper: write only when content actually changes, so an idempotent
    # re-run reports "no changes" instead of "+ Dockerfile" three times.
    def _write_if_changed(path: Path, content: str, label: str) -> None:
        if path.is_file() and path.read_text() == content:
            result.files_skipped.append(f"{label} (no changes)")
            return
        path.write_text(content)
        result.files_written.append(label)

    # Dockerfile — full regen (sentinel-bounded fragments block lives
    # inside the generated content; the whole file is owned by us).
    df_text = render_dockerfile(fragments)
    _write_if_changed(home / "Dockerfile", df_text, "Dockerfile")

    # compose.yml — full regen.
    cy_text = render_compose_yml(service_name=service_name, web_port=web_port)
    _write_if_changed(home / "compose.yml", cy_text, "compose.yml")

    # start.sh — full regen. Always re-chmod (cheap idempotent) so a
    # previously-corrupted mode bit gets restored even when content is
    # unchanged.
    ss_path = home / "start.sh"
    _write_if_changed(ss_path, render_start_sh(uv_extras=uv_extras), "start.sh")
    if ss_path.is_file():
        ss_path.chmod(0o755)

    # compose.env — idempotent merge.
    ce_path = home / "compose.env"
    existing = ce_path.read_text() if ce_path.is_file() else None
    new_env, added = render_compose_env(existing, required_keys)
    if new_env != (existing or ""):
        ce_path.write_text(new_env)
        if existing is None:
            result.files_written.append("compose.env (created)")
        else:
            result.files_written.append("compose.env (merged)")
        result.env_vars_added = added
    else:
        result.files_skipped.append("compose.env (no changes)")

    # .gitignore — append an explicit never-track entry for compose.env
    # so a future operator switching to blocklist-style ignores can't
    # accidentally start committing secrets.
    if _ensure_compose_env_gitignored(home):
        result.files_written.append(".gitignore (appended compose.env block)")

    return result


# ── CLI ─────────────────────────────────────────────────────────────


def add_argparse(parser) -> None:
    """Wire ``mimir scaffold-docker``."""
    parser.add_argument(
        "--home", type=Path, default=None,
        help="Target mimir home (default: $MIMIR_HOME or cwd).",
    )
    parser.add_argument(
        "--service-name", default=None,
        help="Docker compose service / container name "
             "(default: home dir name).",
    )
    parser.add_argument(
        "--web-port", type=int, default=8090,
        help="Host port to bind the agent's HTTP UI on "
             "(127.0.0.1:<port>). Default 8090. Must be in 1–65535.",
    )
    parser.add_argument(
        "--uv-extras", default="",
        help="Comma-separated list of pyproject extras to pass to "
             "``uv sync`` in start.sh (e.g. ``discord,claude-code``). "
             "Default empty — only the base mimir deps get resolved. "
             "Pick the extras your bridges need.",
    )
    parser.set_defaults(scaffold_docker_cmd=cmd)


def _resolve_home(home_arg: Path | None) -> Path:
    """Same precedence as the rest of the CLI: --home > $MIMIR_HOME > cwd."""
    if home_arg is not None:
        return home_arg.resolve()
    env = os.environ.get("MIMIR_HOME")
    if env:
        return Path(env).resolve()
    return Path.cwd().resolve()


def cmd(args) -> int:
    home = _resolve_home(args.home)
    if not home.is_dir():
        print(f"home not a directory: {home}")
        return 2
    if not (1 <= args.web_port <= 65535):
        print(
            f"--web-port {args.web_port} out of range; must be 1–65535."
        )
        return 2
    extras_csv = (getattr(args, "uv_extras", "") or "").strip()
    uv_extras = [x.strip() for x in extras_csv.split(",") if x.strip()] if extras_csv else None
    try:
        result = scaffold(
            home,
            service_name=args.service_name,
            web_port=args.web_port,
            uv_extras=uv_extras,
        )
    except FileNotFoundError as exc:
        print(str(exc))
        return 2

    print(f"scaffolded Docker deploy into {home}:")
    for f in result.files_written:
        print(f"  + {f}")
    for f in result.files_skipped:
        print(f"  = {f}")
    if result.skills_with_fragments:
        print(f"\nincluded dockerfile fragments from {len(result.skills_with_fragments)} skill(s):")
        for s in result.skills_with_fragments:
            print(f"  - {s}")
    if result.env_vars_added:
        print(f"\nappended {len(result.env_vars_added)} env-var placeholder(s) to compose.env:")
        for k in result.env_vars_added:
            print(f"  - {k}")
        print("\nedit compose.env to fill in values. NEVER commit it.")
    print(
        f"\nnext: cd {home}  &&  edit compose.env  &&  "
        f"docker compose build --build-arg USER_UID=$(id -u) "
        f"&&  docker compose up -d"
    )
    return 0
