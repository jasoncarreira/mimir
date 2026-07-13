"""Scaffold container-deployment files for an agent home.

Generates ``Dockerfile``, ``compose.yml``, ``compose.env`` (operator-edited
secrets file), and ``start.sh`` into a mimir home so it can be deployed
in a container the same way mimirbot is. Inspects both
``<home>/skills/`` (operator-installed) and
``<home>/.mimir_builtin_skills/`` (the bundled refresh target) to pick
up per-skill OS-level deps (a skill that needs a system tool ships a
``dockerfile.fragment`` next to its ``SKILL.md``) and per-skill env-var
requirements (``pollers.json`` ``pass_env``). Operator-side entries
shadow bundled same-named entries on collision, matching
``SkillsMiddleware``'s last-source-wins rule.

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
from typing import Literal


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
    Path(__file__).parent / "optional-skills",
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
    """For each skill present in ``<home>/skills/`` OR
    ``<home>/.mimir_builtin_skills/``, look up its
    ``dockerfile.fragment`` — preferring the installed copy in the home,
    falling back to the bundled source (``mimir/skills/<name>/`` or
    ``optional-skills/<name>/``). Ordered alphabetically by skill name
    for stable Dockerfile output.

    Both source dirs get walked so bundled skills with OS deps
    (notably ``chainlink``, which ships a ``dockerfile.fragment``
    that builds ``chainlink-tracker``) get their fragment included
    even when the operator hasn't manually installed them under
    ``skills/``. Dedupe by name — operator-side dirs win on
    collision, matching the rest of the dual-location contract
    (SkillsMiddleware's last-source-wins shadowing).

    The bundled-source fallback for the fragment FILE itself handles
    the older case where a home was seeded before the fragment file
    existed in the bundle: ``seed_skills`` won't refresh an existing
    skill dir, so the operator's home lacks the new file even though
    they have the skill.
    """
    operator_dir = home / "skills"
    builtin_dir = home / ".mimir_builtin_skills"
    seen: dict[str, Path] = {}
    for src in (builtin_dir, operator_dir):
        if not src.is_dir():
            continue
        for skill_dir in src.iterdir():
            if not skill_dir.is_dir() or skill_dir.name.startswith("."):
                continue
            seen[skill_dir.name] = src  # operator dir wins (iterated second)

    out: list[Fragment] = []
    for skill_name in sorted(seen):
        skills_root = seen[skill_name]
        content = _read_fragment(
            skill_name,
            skills_root,  # check the location it was found in first
            *_BUNDLED_SKILL_ROOTS,  # then bundled defaults / optional
        )
        if content is None:
            continue
        out.append(Fragment(skill_name=skill_name, content=content))
    return out


def collect_required_env_vars(home: Path) -> list[str]:
    """Walk ``<home>/skills/*/pollers.json`` AND
    ``<home>/.mimir_builtin_skills/*/pollers.json`` and collect every
    env var listed in ``pass_env`` across all poller skills. De-duped.
    Plus baseline mimir vars (MIMIR_API_KEY, etc.).

    Mirrors :func:`collect_fragments`'s dual-location scan so a bundled
    poller (none ship today, but the architecture allows it) gets its
    ``pass_env`` requirements surfaced into ``compose.env`` without the
    operator having to duplicate the skill into ``<home>/skills/``.
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
        "MIMIR_ENABLE_CLAUDE_CODE",# 1 installs Claude Code CLI + adapter
        "MIMIR_ENABLE_OPENCODE",   # 1 installs + configures OpenCode runtime
    ]
    seen = set(baseline)
    extra: list[str] = []
    for root in (home / ".mimir_builtin_skills", home / "skills"):
        if not root.is_dir():
            continue
        for skill_dir in sorted(root.iterdir()):
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

# Two deployment shapes:
#
# - ``workspace`` (back-compat default): the historic pattern. Container
#   clones the mimir source into ``/workspace/<repo>`` at first boot,
#   runs ``uv sync``, then ``uv run mimir run``. Iterate-locally model
#   — operator's host clone of mimir (or a named volume) supplies the
#   source. mimirbot uses this shape via a hand-rolled Dockerfile;
#   pre-PyPI-publish, all scaffold-docker-generated homes used it too.
#
# - ``pypi`` (new in mimir-agent 0.1.1+): installs ``mimir-agent`` from
#   PyPI into a user-owned venv at image-build time. No source clone,
#   no ``uv sync`` at boot. Plays cleanly with the pending-update flow
#   (the agent's ``request_mimir_update`` tool writes a flag; on next
#   restart, ``pip install --upgrade`` runs in the venv). The right
#   shape for version-pinned production deployments.
#
# Selection: ``mimir scaffold-docker --mode workspace|pypi``. Default
# is ``workspace`` so back-compat reruns on existing homes don't
# silently switch shapes.
ScaffoldMode = Literal["workspace", "pypi"]
_MODES: tuple[ScaffoldMode, ...] = ("workspace", "pypi")
_DEFAULT_MODE: ScaffoldMode = "workspace"


# Shared Dockerfile RUN block — defensive userdel/groupdel before the
# explicit useradd that creates the mimir user. Some skill fragments
# (notably 1Password's apt package) install service users that may
# grab UID 1000 before this layer runs; without the detect-and-delete
# step, the subsequent ``useradd --uid 1000`` fails with exit code 4
# ("UID not unique") and the image build aborts. Both Dockerfile
# templates (workspace + pypi) inline this block via Python
# ``.replace`` so a future fix only edits one place.
#
# Single-quote heredoc-style: contains no Python ``{placeholder}``
# markers so substitution from either template body is a no-op
# beyond the swap.
_USERDEL_BLOCK = '''\
# Defensive: some skill fragments (notably 1Password's apt package)
# install service users that may grab UID 1000 before this layer
# runs. If a non-mimir user occupies our target UID/GID, delete it
# so the explicit useradd below succeeds. Surfaced in the
# muninn-mimir migration (mimir-repo issue #332).
RUN existing_uid_user=$(getent passwd "${USER_UID}" | cut -d: -f1) \\
 && if [ -n "$existing_uid_user" ] && [ "$existing_uid_user" != "mimir" ]; then \\
        echo "[Dockerfile] removing user $existing_uid_user at UID ${USER_UID} to free the slot for mimir"; \\
        userdel "$existing_uid_user" \\
            || echo "[Dockerfile] WARNING: userdel $existing_uid_user failed — subsequent useradd may also fail"; \\
    fi \\
 && existing_gid_group=$(getent group "${USER_GID}" | cut -d: -f1) \\
 && if [ -n "$existing_gid_group" ] && [ "$existing_gid_group" != "mimir" ]; then \\
        echo "[Dockerfile] removing group $existing_gid_group at GID ${USER_GID} to free the slot"; \\
        groupdel "$existing_gid_group" \\
            || echo "[Dockerfile] WARNING: groupdel $existing_gid_group failed — subsequent groupadd may also fail"; \\
    fi'''

_DOCKERFILE_BASE = """\
# Generated by `mimir scaffold-docker`. THIS FILE IS REGENERATED IN
# FULL on every run — do NOT edit it in place; your changes WILL be
# lost the next time `mimir scaffold-docker` runs.
#
# To customize the build for this deployment, add a separate layer
# (e.g. ``Dockerfile.custom``) and reference it from compose.yml, or
# author a per-skill ``dockerfile.fragment`` under
# ``<home>/skills/<name>/`` — those get stitched into the
# sentinel-marked block below on each regeneration.
#
# Build: docker compose build --build-arg USER_UID=$(id -u)
# Run:   docker compose up -d

FROM python:3.11-slim

# Base system tooling — same set mimirbot has been running with since
# 2026-05: git for the source clone + agent dev loop, gh for PR
# creation, build-essential for any C extensions, ca-certificates for
# HTTPS clones, poppler-utils for PDF ingest, Node 22 (needed for
# optional Claude Code CLI plus mermaid-cli), jq for JSONL log parsing
# across many skill bodies + the introspection recipes.
RUN apt-get update \\
 && apt-get install -y --no-install-recommends \\
        git \\
        tini \\
        curl \\
        ca-certificates \\
        build-essential \\
        gnupg \\
        tmux \\
        poppler-utils \\
        jq \\
        ripgrep \\
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \\
        | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \\
 && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \\
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \\
        > /etc/apt/sources.list.d/github-cli.list \\
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \\
 && apt-get install -y --no-install-recommends gh nodejs \\
 && rm -rf /var/lib/apt/lists/*

# Pinned mermaid CLI (used by mermaid-diagrams skill).
RUN npm install -g @mermaid-js/mermaid-cli@11.16.0

__CLAUDE_CODE_INSTALL__

__CODEX_INSTALL__

__OPENCODE_INSTALL__

__OPENCODE_CONFIG__

# uv handles dep resolution + virtualenv inside the workspace clone.
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \\
 && mv /root/.local/bin/uv /usr/local/bin/uv \\
 && mv /root/.local/bin/uvx /usr/local/bin/uvx

# ===== BEGIN mimir-scaffold-docker: skill fragments =====
# Auto-generated from <home>/skills/*/dockerfile.fragment.
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
__USERDEL_BLOCK__
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

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/start.sh"]
"""


_DOCKERFILE_BASE_PYPI = """\
# Generated by `mimir scaffold-docker --mode pypi`. THIS FILE IS
# REGENERATED IN FULL on every run — do NOT edit it in place; your
# changes WILL be lost the next time `mimir scaffold-docker` runs.
#
# Mode: pypi. mimir-agent installs from PyPI into a user-owned venv
# at image-build time; the pending-update flow (operator-approved
# upgrade via the ``request_mimir_update`` tool) does
# ``pip install --upgrade`` against that venv on next restart.
#
# Build: docker compose build --build-arg USER_UID=$(id -u)
# Run:   docker compose up -d

FROM python:3.11-slim

# Base system tooling — git for any local commits the agent makes,
# gh for PR / issue automation, build-essential for C extensions
# pulled by deps, poppler-utils + jq because skill bodies use them,
# Node 22 for the optional claude-code CLI + mermaid CLI.
RUN apt-get update \\
 && apt-get install -y --no-install-recommends \\
        git \\
        tini \\
        curl \\
        ca-certificates \\
        build-essential \\
        gnupg \\
        tmux \\
        poppler-utils \\
        jq \\
        ripgrep \\
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \\
        | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \\
 && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \\
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \\
        > /etc/apt/sources.list.d/github-cli.list \\
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \\
 && apt-get install -y --no-install-recommends gh nodejs \\
 && rm -rf /var/lib/apt/lists/*

# Pinned mermaid CLI (used by mermaid-diagrams skill).
RUN npm install -g @mermaid-js/mermaid-cli@11.16.0

__CLAUDE_CODE_INSTALL__

__CODEX_INSTALL__

__OPENCODE_INSTALL__

__OPENCODE_CONFIG__

# (No ``uv`` install — PyPI mode uses pip directly against a
# user-owned venv. Saves ~30 MB of image and one moving part.)

# ===== BEGIN mimir-scaffold-docker: skill fragments =====
# Auto-generated from <home>/skills/*/dockerfile.fragment.
# Each skill that ships a fragment contributes one block below.
# Re-run `mimir scaffold-docker` to refresh after installing /
# uninstalling skills.
""" + _FRAGMENTS_SENTINEL + """
# ===== END mimir-scaffold-docker: skill fragments =====

# Non-root user. Claude Code refuses --dangerously-skip-permissions
# when uid 0; the bind-mounted ``/mimir-home`` also needs to match
# the host UID so the operator can read/edit agent state without
# sudo. ``docker compose build --build-arg USER_UID=$(id -u)``.
ARG USER_UID=1000
ARG USER_GID=1000
__USERDEL_BLOCK__
RUN groupadd --gid "${USER_GID}" mimir \\
 && useradd --uid "${USER_UID}" --gid "${USER_GID}" --create-home --shell /bin/bash mimir \\
 && mkdir -p /mimir-home \\
 && chown -R mimir:mimir /mimir-home

USER mimir
WORKDIR /home/mimir

# User-owned venv. The pending-update flag flow runs
# ``pip install --upgrade mimir-agent`` against THIS venv at next
# restart, then ``os.execv``'s onto the new code. User ownership
# is what makes that work without root.
ENV VIRTUAL_ENV=/home/mimir/venv
RUN python3 -m venv "$VIRTUAL_ENV"
ENV PATH="$VIRTUAL_ENV/bin:/usr/local/bin:${PATH}"

# Install mimir-agent from PyPI. Extras default to the scaffold-time
# choice (--extras flag); operators can override at build time via:
#
#   docker build --build-arg MIMIR_EXTRAS=anthropic,discord .
#
# Available extras (mimir-agent pyproject):
#   anthropic, claude-code, openai, codex-plus    (model providers)
#   discord, slack                   (bridges)
#   mcp                              (Model Context Protocol)
#
# Set MIMIR_ENABLE_CLAUDE_CODE=1 below to install both the Claude Code
# CLI and the adapter extra in one build switch.
ARG MIMIR_EXTRAS="__MIMIR_EXTRAS__"
RUN pip install --no-cache-dir --upgrade pip \\
 && pip install --no-cache-dir "mimir-agent[${MIMIR_EXTRAS}]"

# Optional: install the Claude Code subprocess provider adapter. Set
# ``MIMIR_ENABLE_CLAUDE_CODE=1`` to enable; the same build arg installs
# the npm CLI above.
RUN if [ "$MIMIR_ENABLE_CLAUDE_CODE" = "1" ]; then \\
        pip install --no-cache-dir "mimir-agent[claude-code]" ; \\
    fi

# Pre-warm the fastembed model cache so the first request doesn't
# pay the ~80MB download. Skipped silently if offline at build time.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='BAAI/bge-small-en-v1.5')" || true

ENV MIMIR_HOME=/mimir-home

EXPOSE 8080

COPY --chown=mimir:mimir start.sh /usr/local/bin/start.sh
# Source file must be executable in the repo (committed +x); we
# can't chmod after copying since USER mimir doesn't own /usr/local/bin.

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/start.sh"]
"""


def _claude_code_install_block() -> str:
    """Dockerfile block installing the Claude Code CLI when enabled.

    The npm CLI is needed only for the Claude Code subprocess
    provider. Gate it on the same build arg as the
    ``mimir-agent[claude-code]`` Python provider so Codex/OpenAI/Anthropic
    API deployments don't carry an unused CLI binary.
    """
    return (
        "# Claude Code CLI — optional subprocess-provider transport.\n"
        "# Same gate as the langchain-claude-code-mimir Python provider below.\n"
        "ARG MIMIR_ENABLE_CLAUDE_CODE=0\n"
        "RUN if [ \"$MIMIR_ENABLE_CLAUDE_CODE\" = \"1\" ]; then \\\n"
        "        npm install -g @anthropic-ai/claude-code@2.1.206 ; \\\n"
        "    fi"
    )


def _codex_install_block(install_codex: bool) -> str:
    """Dockerfile line installing the codex CLI, or a placeholder comment.

    Installed for codex-subscription deployments (the ``codex-plus``
    extra) so ``spawn_codex`` can shell out to ``codex exec`` and Codex
    Plus auth (``~/.codex/auth.json``) is usable. Same npm-global pattern
    as the optional claude-code CLI.
    """
    if not install_codex:
        return "# (codex CLI not installed — no codex-plus extra selected)"
    return (
        "# Codex CLI — codex-subscription deployments (codex-plus extra).\n"
        "# spawn_codex shells out to ``codex exec``; Codex Plus auth lives\n"
        "# at ~/.codex/auth.json. npm-global, like the claude-code CLI above.\n"
        "RUN npm install -g @openai/codex@0.144.1"
    )


def _opencode_install_block(install_opencode: bool) -> str:
    """Dockerfile block installing OpenCode runtime when enabled.

    Installs the pinned OpenCode runtime (opencode-ai + plugins) when
    MIMIR_ENABLE_OPENCODE=1. The plugins provide feature-factory and
    project-memory functionality for Worklink backends.
    """
    if not install_opencode:
        return "# (OpenCode runtime not installed — MIMIR_ENABLE_OPENCODE not set)"
    return (
        "# OpenCode runtime — opt-in via MIMIR_ENABLE_OPENCODE=1.\n"
        "# Pins: opencode-ai@1.17.15, opencode-feature-factory@0.2.1,\n"
        "# opencode-project-memory@0.1.0, opencode-openai-codex-auth@4.4.0,\n"
        "# opencode-anthropic-auth@0.0.13.\n"
        "ARG MIMIR_ENABLE_OPENCODE=0\n"
        "RUN if [ \"$MIMIR_ENABLE_OPENCODE\" = \"1\" ]; then \\\n"
        "        npm install -g opencode-ai@1.17.15 ; \\\n"
        "        npm install -g opencode-feature-factory@0.2.1 ; \\\n"
        "        npm install -g opencode-project-memory@0.1.0 ; \\\n"
        "        npm install -g opencode-openai-codex-auth@4.4.0 ; \\\n"
        "        npm install -g opencode-anthropic-auth@0.0.13 ; \\\n"
        "    fi"
    )


def render_dockerfile(
    fragments: list[Fragment],
    *,
    mode: ScaffoldMode = _DEFAULT_MODE,
    mimir_extras: list[str] | None = None,
    install_codex: bool = False,
    install_opencode: bool = False,
) -> str:
    """Stitch fragments into the base template via sentinel split, so
    a fragment that happens to contain the literal sentinel-string
    can't corrupt the substitution (the split-based insert leaves the
    sentinel unique to the template's own copy).

    ``mode`` selects between the workspace (clone + ``uv sync``) and
    pypi (``pip install mimir-agent``) shapes. ``mimir_extras`` is
    used in pypi mode to set the default ``MIMIR_EXTRAS`` build-arg;
    ignored in workspace mode (extras flow through ``start.sh`` via
    ``UV_EXTRAS`` there).
    """
    if mode == "pypi":
        base = _DOCKERFILE_BASE_PYPI
        extras_csv = ",".join(mimir_extras) if mimir_extras else "anthropic,discord,slack,mcp"
        # Use ``__MIMIR_EXTRAS__`` not ``{MIMIR_EXTRAS}`` as the
        # placeholder: ``str.replace`` is substring-based, and the
        # template ALSO contains the shell expansion ``${MIMIR_EXTRAS}``
        # on the ``pip install`` line. A ``{MIMIR_EXTRAS}`` placeholder
        # would match inside that shell ref too, leaving a stray
        # ``$<extras-list>`` that the shell would interpret as a
        # variable reference at build time. The double-underscore
        # marker is safe — it can't appear inside ``${VAR}``.
        base = base.replace("__MIMIR_EXTRAS__", extras_csv)
    elif mode == "workspace":
        base = _DOCKERFILE_BASE
    else:
        raise ValueError(f"unknown scaffold mode {mode!r}; expected one of {_MODES}")
    # Shared userdel/groupdel block — inlined here so the workspace
    # and pypi templates can't drift on the defensive cleanup logic.
    base = base.replace("__USERDEL_BLOCK__", _USERDEL_BLOCK)
    base = base.replace("__CLAUDE_CODE_INSTALL__", _claude_code_install_block())
    base = base.replace("__CODEX_INSTALL__", _codex_install_block(install_codex))
    base = base.replace("__OPENCODE_INSTALL__", _opencode_install_block(install_opencode))
    base = base.replace("__OPENCODE_CONFIG__", _opencode_build_config_block(install_opencode))
    if not fragments:
        body = "# (no skills installed yet ship a dockerfile.fragment)"
    else:
        parts = []
        for f in fragments:
            parts.append(f"# --- {f.skill_name} ---\n{f.content.strip()}")
        body = "\n\n".join(parts)
    # Use split(..., 1) + join so even a malformed template (sentinel
    # appearing twice for some reason) doesn't multi-paste the body.
    head, sep, tail = base.partition(_FRAGMENTS_SENTINEL)
    if not sep:
        raise RuntimeError(
            "scaffold_docker: base template is missing the FRAGMENTS "
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
    build:
      context: .
      args:
        # One switch for Claude Code support: installs the npm CLI at
        # build time and makes workspace start.sh sync --extra claude-code.
        MIMIR_ENABLE_CLAUDE_CODE: ${MIMIR_ENABLE_CLAUDE_CODE:-0}
        # Install the pinned OpenCode runtime + plugins. start.sh merges
        # their config into OpenCode's XDG path when the same flag is set.
        MIMIR_ENABLE_OPENCODE: ${MIMIR_ENABLE_OPENCODE:-0}
    container_name: {SERVICE_NAME}
    restart: unless-stopped
    # chainlink #510: give the graceful drain time to finish in-flight turns
    # on stop/restart before Docker SIGKILLs. Keep this >= MIMIR_DRAIN_TIMEOUT_SECONDS
    # (default 30s); Docker's default grace is only 10s, which would cut the
    # drain off mid-turn.
    stop_grace_period: 45s
    env_file:
      - compose.env
    environment:
      MIMIR_HOME: /mimir-home
      MIMIR_WEB_PORT: 8080
      # Inside-container bind. Must be 0.0.0.0 so Docker's port-forward
      # (see ``ports:`` below) can reach the app — mimir's default is
      # 127.0.0.1 (PR #323, defense-in-depth on host installs), which
      # in a container only listens on container-loopback and produces
      # a silent "Empty reply from server" through the host-side
      # forward. Host exposure stays loopback-only via the
      # "127.0.0.1:..." binding in ``ports:``; MIMIR_API_KEY gates
      # the endpoint either way.
      MIMIR_WEB_HOST: 0.0.0.0
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


_COMPOSE_YML_TEMPLATE_PYPI = """\
# Generated by `mimir scaffold-docker --mode pypi`. REGENERATED IN FULL
# on every run — edits WILL be lost. Use a docker-compose override
# file for per-deployment customization:
# https://docs.docker.com/compose/extends/
#
# Mode: pypi. mimir-agent installs from PyPI at image-build time; no
# source clone. The pending-update flow (operator approves via the
# ``request_mimir_update`` tool) writes a flag to
# ``./.mimir/pending-update.flag`` — next ``docker compose restart``
# runs ``pip install --upgrade`` against the venv.
#
# Usage:
#   cp compose.env.example compose.env  &&  edit compose.env
#   docker compose up -d --build
#   docker compose logs -f

services:
  {SERVICE_NAME}:
    build:
      context: .
      args:
        # One switch for Claude Code support: installs the npm CLI and
        # mimir-agent[claude-code] adapter extra at build time.
        MIMIR_ENABLE_CLAUDE_CODE: ${MIMIR_ENABLE_CLAUDE_CODE:-0}
        # Install the pinned OpenCode runtime + plugins. start.sh merges
        # their config into OpenCode's XDG path when the same flag is set.
        MIMIR_ENABLE_OPENCODE: ${MIMIR_ENABLE_OPENCODE:-0}
    container_name: {SERVICE_NAME}
    restart: unless-stopped
    # chainlink #510: give the graceful drain time to finish in-flight turns
    # on stop/restart before Docker SIGKILLs. Keep this >= MIMIR_DRAIN_TIMEOUT_SECONDS
    # (default 30s); Docker's default grace is only 10s, which would cut the
    # drain off mid-turn.
    stop_grace_period: 45s
    env_file:
      - compose.env
    environment:
      MIMIR_HOME: /mimir-home
      MIMIR_WEB_PORT: 8080
      # Inside-container bind. Must be 0.0.0.0 so Docker's port-forward
      # (see ``ports:`` below) can reach the app — mimir's default is
      # 127.0.0.1 (PR #323, defense-in-depth on host installs), which
      # in a container only listens on container-loopback and produces
      # a silent "Empty reply from server" through the host-side
      # forward. Host exposure stays loopback-only via the
      # "127.0.0.1:..." binding in ``ports:``; MIMIR_API_KEY gates
      # the endpoint either way.
      MIMIR_WEB_HOST: 0.0.0.0
    ports:
      # 127.0.0.1 binding — defense in depth alongside the auth
      # middleware. To expose on LAN, change to "0.0.0.0:{WEB_PORT}:8080".
      - "127.0.0.1:{WEB_PORT}:8080"
    volumes:
      # Persistent agent state — saga.db, logs, identities.yaml,
      # pending-update flag, etc. The current dir IS /mimir-home.
      # .gitignore controls what the auto-push (if enabled) tracks.
      - .:/mimir-home
"""


def render_compose_yml(
    *,
    service_name: str,
    web_port: int,
    mode: ScaffoldMode = _DEFAULT_MODE,
) -> str:
    """``mode='workspace'`` (default) emits the clone-on-boot shape;
    ``mode='pypi'`` drops the ``workspace`` named volume so the
    container builds from the PyPI-installed mimir-agent in the
    user-owned venv."""
    if mode == "pypi":
        tmpl = _COMPOSE_YML_TEMPLATE_PYPI
    elif mode == "workspace":
        tmpl = _COMPOSE_YML_TEMPLATE
    else:
        raise ValueError(f"unknown scaffold mode {mode!r}; expected one of {_MODES}")
    return (
        tmpl
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
# Idempotent: only register safe.directory when it's not already there.
# A bare ``--add`` runs on every container start, so without this guard
# ~/.gitconfig accumulates a duplicate ``safe.directory`` line per boot.
git config --global --get-all safe.directory 2>/dev/null \
    | grep -qxF "${REPO_DIR}" \
    || git config --global --add safe.directory "${REPO_DIR}"

# ─── deps sync ─────────────────────────────────────────────────────
# Extras templated from ``mimir scaffold-docker --uv-extras`` so the
# right pyproject.toml ``[project.optional-dependencies]`` get
# resolved at boot. Override per-deployment if your bridges need
# different extras (slack, etc.).
UV_EXTRAS="{UV_EXTRAS}"
if [ "${MIMIR_ENABLE_CLAUDE_CODE:-0}" = "1" ]; then
    UV_EXTRAS="$UV_EXTRAS --extra claude-code"
fi
echo "[start.sh] uv sync (extras: ${UV_EXTRAS:-(none)})"
uv sync $UV_EXTRAS

# ─── skill-required extras (chainlink #406) ────────────────────────
# Optional skills can declare ``requires_extras`` in their SKILL.md
# (e.g. the gepa optimizer needs the ``gepa`` extra). Without folding
# them in, the base sync above PRUNES a skill's dependency on every
# restart. The base sync already made ``mimir`` importable, so query it
# for what the installed skills need and re-sync with them included.
# Best-effort: if the query fails we keep the base extras and carry on.
SKILL_EXTRAS="$(uv run mimir skills required-extras --home "${MIMIR_HOME}" --as-uv-flags 2>/dev/null || true)"
if [ -n "${SKILL_EXTRAS}" ]; then
    echo "[start.sh] + skill-required extras: ${SKILL_EXTRAS}"
    uv sync $UV_EXTRAS $SKILL_EXTRAS
fi

# ─── home seed (idempotent — only writes missing files) ────────────
mkdir -p "${MIMIR_HOME}"
echo "[start.sh] mimir setup (idempotent)"
uv run mimir setup --home "${MIMIR_HOME}" || {
    echo "[start.sh] WARNING: mimir setup non-zero — home may be partial" >&2
}
if [ "${MIMIR_ENABLE_OPENCODE:-0}" = "1" ]; then
    echo "[start.sh] merging OpenCode plugin config into the XDG config path"
    uv run mimir opencode-bootstrap --home "$HOME"
fi

# ─── run ───────────────────────────────────────────────────────────
echo "[start.sh] starting mimir run (home=${MIMIR_HOME}, port=${MIMIR_WEB_PORT:-8080})"
exec uv run mimir run --home "${MIMIR_HOME}"
"""


_START_SH_TEMPLATE_PYPI = """\
#!/usr/bin/env bash
# Generated by `mimir scaffold-docker --mode pypi`. REGENERATED IN FULL
# on every run — do NOT edit in place; your changes WILL be lost. For
# per-deployment behavior, set env vars in compose.env (GH_USER_NAME,
# GH_USER_EMAIL, GITHUB_TOKEN, etc.).
#
# Mode: pypi. mimir-agent is already installed in the image's
# user-owned venv (``/home/mimir/venv``) at build time, so start.sh
# only does:
#   1. git + gh auth from GITHUB_TOKEN (skill calls need gh).
#   2. mimir setup --home /mimir-home (idempotent; only writes
#      missing files).
#   3. exec mimir run.
#
# The pending-update flow runs BEFORE step 3 inside ``mimir run``'s
# pre-flight (server.main), so a ``request_mimir_update`` flag gets
# acted on at container boot without start.sh needing to know.
set -euo pipefail

# ─── git + gh auth ─────────────────────────────────────────────────
# Identity for any commits mimir makes from inside the container
# (e.g. auto-commit of /mimir-home state). Overridable in compose.env.
git config --global user.name  "${GH_USER_NAME:-mimir-agent}"
git config --global user.email "${GH_USER_EMAIL:-mimir-agent@local}"
git config --global init.defaultBranch main

# gh CLI auth from the operator-supplied PAT. Skills that talk to
# GitHub (chainlink, github poller, etc.) need this.
if [ -n "${GITHUB_TOKEN:-}" ]; then
    if ! gh auth status >/dev/null 2>&1; then
        echo "${GITHUB_TOKEN}" | gh auth login --with-token
    fi
    gh auth setup-git >/dev/null 2>&1 || true
else
    echo "[start.sh] WARNING: GITHUB_TOKEN unset — gh-based skills will fail"
fi

# ─── home seed (idempotent — only writes missing files) ────────────
mkdir -p "${MIMIR_HOME}"
echo "[start.sh] mimir setup (idempotent)"
mimir setup --home "${MIMIR_HOME}" || {
    echo "[start.sh] WARNING: mimir setup non-zero — home may be partial" >&2
}
if [ "${MIMIR_ENABLE_OPENCODE:-0}" = "1" ]; then
    echo "[start.sh] merging OpenCode plugin config into the XDG config path"
    mimir opencode-bootstrap --home "$HOME"
fi

# ─── run ───────────────────────────────────────────────────────────
echo "[start.sh] starting mimir run (home=${MIMIR_HOME}, port=${MIMIR_WEB_PORT:-8080})"
exec mimir run --home "${MIMIR_HOME}"
"""


def render_start_sh(
    *,
    uv_extras: list[str] | None = None,
    mode: ScaffoldMode = _DEFAULT_MODE,
) -> str:
    """Render start.sh.

    Workspace mode: takes ``uv_extras`` (list of extra-names like
    ``["discord", "claude-code"]``) and expands them to the
    ``--extra <name>`` flags ``uv sync`` expects.

    PyPI mode: ``uv_extras`` is ignored — mimir-agent's extras are
    chosen at image-build time via the Dockerfile's ``MIMIR_EXTRAS``
    build-arg, not at container boot. start.sh in PyPI mode doesn't
    have a uv sync step.

    **Maintenance constraint** (workspace mode only) — the template
    uses two superimposed substitution layers: this Python
    ``.replace()`` at scaffold time *and* bash variable expansion at
    container boot. When adding a new Python placeholder
    (e.g. ``{NEW_VAR}``), use **bare ``$NEW_VAR``, NOT ``${NEW_VAR}``**
    in any shell reference to the same value elsewhere in the
    template — otherwise ``.replace("{NEW_VAR}", "")`` matches the
    substring inside ``${NEW_VAR}`` and leaves a stray ``$`` that
    crashes the container at boot. See the regression test
    ``test_render_start_sh_uv_sync_line_is_valid_shell_with_no_extras``.
    """
    if mode == "pypi":
        return _START_SH_TEMPLATE_PYPI
    if mode != "workspace":
        raise ValueError(f"unknown scaffold mode {mode!r}; expected one of {_MODES}")
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


_OPENCODE_CONFIG_TEMPLATE = """\
{
  "$schema": "https://opencode.ai/config.json",
  "plugin": [
    "opencode-feature-factory",
    [
      "opencode-project-memory",
      {
        "memoryDir": ".opencode/memory",
        "index": "MEMORY.md",
        "maxIndexBytes": 8192,
        "maxIndexLines": 100,
        "gitExclude": false
      }
    ],
    "opencode-openai-codex-auth",
    "opencode-anthropic-auth"
  ]
}
"""

_OPENCODE_REQUIRED_PLUGINS: tuple[object, ...] = (
    "opencode-feature-factory",
    (
        "opencode-project-memory",
        {
            "memoryDir": ".opencode/memory",
            "index": "MEMORY.md",
            "maxIndexBytes": 8192,
            "maxIndexLines": 100,
            "gitExclude": False,
        },
    ),
    "opencode-openai-codex-auth",
    "opencode-anthropic-auth",
)


def _render_opencode_config() -> str:
    """Render the canonical config used when no OpenCode config exists."""
    return _OPENCODE_CONFIG_TEMPLATE


def _opencode_build_config_block(install_opencode: bool) -> str:
    """Bake global config into generated images under OpenCode's XDG path."""
    if not install_opencode:
        return "# (OpenCode config not baked — MIMIR_ENABLE_OPENCODE not set)"
    payload = _OPENCODE_CONFIG_TEMPLATE.replace("\\", "\\\\").replace("%", "%%")
    return (
        "# Global OpenCode config: loaded from outer repos and Worklink worktrees.\n"
        "RUN mkdir -p /home/mimir/.config/opencode \\\n"
        f" && printf '%s' '{payload}' > /home/mimir/.config/opencode/opencode.json \\\n"
        " && chown -R mimir:mimir /home/mimir/.config"
    )


def _plugin_name(entry: object) -> str | None:
    """Return a plugin package name from OpenCode's string/tuple syntax."""
    if isinstance(entry, str):
        return entry
    if (
        isinstance(entry, (list, tuple))
        and entry
        and isinstance(entry[0], str)
    ):
        return entry[0]
    return None


def _required_plugin_entry(entry: object) -> object:
    """Convert the internal immutable plugin declaration to JSON values."""
    if isinstance(entry, tuple):
        return [entry[0], dict(entry[1])]
    return entry


def merge_opencode_config(existing: str | None) -> tuple[str, bool]:
    """Add mimir's pinned plugins without clobbering operator config.

    OpenCode accepts plugin registrations as either package-name strings or
    ``[package, options]`` tuples. Existing feature-factory registrations win
    wholesale, including profile tuples/options. Project-memory preserves
    unrelated operator options while converging mimir's managed memory keys.
    Duplicate registrations are collapsed by package name while preserving the
    first entry and the relative order of all unrelated plugins.
    """
    if existing is None:
        return _render_opencode_config(), True

    try:
        config = json.loads(existing)
    except json.JSONDecodeError as exc:
        raise ValueError(f"existing OpenCode config is not valid JSON: {exc}") from exc
    if not isinstance(config, dict):
        raise ValueError("existing OpenCode config must be a JSON object")

    raw_plugins = config.get("plugin", [])
    if raw_plugins is None:
        raw_plugins = []
    if not isinstance(raw_plugins, list):
        raise ValueError("existing OpenCode config 'plugin' must be a JSON array")

    plugins: list[object] = []
    seen: set[str] = set()
    for entry in raw_plugins:
        name = _plugin_name(entry)
        if name is not None:
            if name in seen:
                continue
            seen.add(name)
        plugins.append(entry)

    for required in _OPENCODE_REQUIRED_PLUGINS:
        name = _plugin_name(required)
        assert name is not None
        if name not in seen:
            plugins.append(_required_plugin_entry(required))
            seen.add(name)
            continue
        if name == "opencode-project-memory":
            # These values are the runtime contract, not optional defaults.
            # Preserve unrelated operator options while converging the six
            # managed keys to the values needed for tracked project memory.
            index = next(
                i for i, entry in enumerate(plugins)
                if _plugin_name(entry) == name
            )
            current = plugins[index]
            operator_options = (
                dict(current[1])
                if isinstance(current, list)
                and len(current) > 1
                and isinstance(current[1], dict)
                else {}
            )
            managed_options = dict(required[1])
            operator_options.update(managed_options)
            plugins[index] = [name, operator_options]

    config["plugin"] = plugins
    merged = json.dumps(config, indent=2, ensure_ascii=False) + "\n"
    return merged, merged != existing


def ensure_opencode_config(config_path: Path) -> bool:
    """Create or preserving-merge the resolved OpenCode config path."""
    existing = config_path.read_text(encoding="utf-8") if config_path.is_file() else None
    merged, changed = merge_opencode_config(existing)
    if changed:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(merged, encoding="utf-8")
    return changed


def opencode_config_path(home: Path) -> Path:
    """Resolved global config OpenCode reads from every project/worktree."""
    return home / ".config" / "opencode" / "opencode.json"


def bootstrap_opencode_config(home: Path) -> bool:
    """Install the global plugin config under OpenCode's XDG search path."""
    return ensure_opencode_config(opencode_config_path(home))


def scaffold(
    home: Path,
    *,
    service_name: str | None = None,
    web_port: int = 8090,
    uv_extras: list[str] | None = None,
    mode: ScaffoldMode = _DEFAULT_MODE,
    mimir_extras: list[str] | None = None,
    install_opencode: bool = False,
) -> ScaffoldResult:
    """Generate / refresh Docker scaffolding for an agent home.

    Idempotent: re-running picks up new skill fragments / env vars
    without clobbering operator-edited values in compose.env.

    ``mode`` selects between ``workspace`` (clone-on-boot + ``uv sync``;
    back-compat default) and ``pypi`` (``pip install mimir-agent`` into
    a user-owned venv at image-build; required for the pending-update
    flow). See the module-level docstring for the full trade-off.

    ``uv_extras`` are passed through to workspace-mode ``start.sh`` so
    ``uv sync`` resolves the right ``[project.optional-dependencies]``
    extras at boot. Ignored in pypi mode — see ``mimir_extras``.

    ``mimir_extras`` sets the default ``MIMIR_EXTRAS`` build-arg in
    the pypi-mode Dockerfile, so ``pip install mimir-agent[...]``
    picks them up. Operators can also override at build time via
    ``docker build --build-arg MIMIR_EXTRAS=...``. Default
    ``["anthropic", "discord", "slack", "mcp"]`` matches the common
    multi-bridge production deployment. Ignored in workspace mode.
    """
    if mode not in _MODES:
        raise ValueError(f"unknown scaffold mode {mode!r}; expected one of {_MODES}")
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
    # Codex-subscription deployments (the ``codex-plus`` extra) need the
    # codex CLI in the image — spawn_codex shells out to ``codex exec`` and
    # Codex Plus auth uses it. Detect from whichever extras apply to the
    # mode (pypi bakes mimir_extras into the image; workspace passes
    # uv_extras to ``uv sync``).
    _mode_extras = (mimir_extras if mode == "pypi" else uv_extras) or []
    install_codex = "codex-plus" in _mode_extras
    df_text = render_dockerfile(
        fragments, mode=mode, mimir_extras=mimir_extras, install_codex=install_codex,
        install_opencode=install_opencode,
    )
    _write_if_changed(home / "Dockerfile", df_text, "Dockerfile")

    # compose.yml — full regen.
    cy_text = render_compose_yml(
        service_name=service_name, web_port=web_port, mode=mode,
    )
    _write_if_changed(home / "compose.yml", cy_text, "compose.yml")

    # start.sh — full regen. Always re-chmod (cheap idempotent) so a
    # previously-corrupted mode bit gets restored even when content is
    # unchanged.
    ss_path = home / "start.sh"
    _write_if_changed(
        ss_path, render_start_sh(uv_extras=uv_extras, mode=mode), "start.sh",
    )
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

    # OpenCode config — idempotent preserving merge.  This is the XDG path
    # OpenCode reads before walking project-local configs, so the same plugin
    # registrations apply from the outer repo and every Worklink worktree.
    if install_opencode:
        oc_path = opencode_config_path(home)
        existed = oc_path.is_file()
        if ensure_opencode_config(oc_path):
            action = "merged" if existed else "created"
            result.files_written.append(f".config/opencode/opencode.json ({action})")
        else:
            result.files_skipped.append(
                ".config/opencode/opencode.json (no changes)"
            )

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
        "--mode", choices=list(_MODES), default=_DEFAULT_MODE,
        help=(
            "Deployment shape. ``workspace`` (default, back-compat) "
            "clones mimir source at boot + runs ``uv sync``. ``pypi`` "
            "installs mimir-agent from PyPI into a user-owned venv at "
            "image-build time — required for the pending-update flow "
            "to work. Run ``mimir scaffold-docker --mode pypi`` on an "
            "existing workspace-mode home to migrate."
        ),
    )
    parser.add_argument(
        "--uv-extras", default="",
        help=(
            "Comma-separated list of pyproject extras passed to "
            "``uv sync`` in workspace-mode start.sh (e.g. "
            "``discord,claude-code``). Default empty. IGNORED in "
            "pypi mode — use ``--extras`` (or override the "
            "``MIMIR_EXTRAS`` Docker build-arg) instead."
        ),
    )
    parser.add_argument(
        "--extras", default="",
        help=(
            "Comma-separated list of mimir-agent extras baked into "
            "the pypi-mode Dockerfile via the ``MIMIR_EXTRAS`` "
            "build-arg (e.g. ``anthropic,discord,slack,mcp``). "
            "Default ``anthropic,discord,slack,mcp``. IGNORED in "
            "workspace mode — use ``--uv-extras`` there."
        ),
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
    uv_extras_csv = (getattr(args, "uv_extras", "") or "").strip()
    uv_extras = [x.strip() for x in uv_extras_csv.split(",") if x.strip()] if uv_extras_csv else None
    mimir_extras_csv = (getattr(args, "extras", "") or "").strip()
    mimir_extras = (
        [x.strip() for x in mimir_extras_csv.split(",") if x.strip()]
        if mimir_extras_csv else None
    )
    mode = getattr(args, "mode", _DEFAULT_MODE)
    try:
        result = scaffold(
            home,
            service_name=args.service_name,
            web_port=args.web_port,
            uv_extras=uv_extras,
            mode=mode,
            mimir_extras=mimir_extras,
        )
    except (FileNotFoundError, ValueError) as exc:
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
