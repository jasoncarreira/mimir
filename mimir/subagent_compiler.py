"""Compile skills with declared ``allowed-tools`` into SubAgent specs.

Per `docs/skill-as-tool-architecture.md`: a skill that declares
``allowed-tools`` in frontmatter should run as a *subagent* — a focused
sub-conversation with a restricted tool surface and a structured
return — rather than be loaded inline into the parent's context. This
module scans the configured skills directories and produces
:class:`SubAgent` specs that the caller passes to
``create_deep_agent(subagents=...)``.

Routing decision per skill, made at startup:

1. Skill declares ``allowed-tools`` in frontmatter → compile to
   :class:`SubAgent`, surface via the framework's ``task`` tool.
2. Skill does NOT declare ``allowed-tools`` → stays inline, rendered
   in mimir's normal skill catalog.

Optional frontmatter fields (delegatable skills only):

- ``params`` — JSON Schema dict declaring what the parent agent
  should pass via the task description. Rendered as a
  ``## Parameters`` block in the subagent's system_prompt;
  summarized into the SubAgent description so the parent sees the
  expected shape before invoking.
- ``returns`` — JSON Schema dict for the expected output shape.
  Passed to deepagents' ``response_format=`` field, which enforces
  the schema on the subagent's last message. Parent receives
  structured ``tool_result`` content matching this shape.

Both are optional — skills compiled without them inherit today's
free-text behavior (task description is a single string; result is
the last assistant message).

**Reflective override (spike-era).** A small set of skills declare
``allowed-tools`` for documentation purposes but actually need the
parent's in-context reasoning to operate (per the heuristic in
``docs/skill-as-tool-architecture.md`` OQ #4). These are excluded
from compilation here via a hardcoded set so they keep their inline
behavior. The proper long-term mechanism is an ``inline: true``
frontmatter flag — that lands in a follow-up; this set is the
bridge.

**Tool resolution.** ``allowed-tools`` entries split into two
groups:

- *Framework built-ins* (Bash, Read, Write, Edit, Glob, Grep, etc.)
  — auto-injected into every SubAgent's middleware stack by deepagents.
  We don't have to list them in the SubAgent's ``tools=`` field;
  they come for free.
- *mimir-custom tools* (lowercase snake_case names like
  ``memory_store``, ``send_message``, ``saga_feedback``, etc.) —
  must be resolved against the parent's tool registry and listed
  explicitly so the subagent has access.

Unknown names (typos, dropped tools, drift) are logged at WARNING
and silently dropped — better to compile the skill with a smaller
surface than to crash startup over stale frontmatter. Tool-set
drift is real (e.g. one muninn skill currently declares
``saga_store`` which no longer exists in the registry).

**Failure mode.** If skill compilation throws on any individual
skill, that skill is skipped and a WARNING is logged. Startup
continues. We never let a malformed SKILL.md block agent boot.

**Algedonic signal.** When ``allowed-tools`` declares names that
don't resolve against mimir's tool registry (typo, dropped tool,
post-rename drift), the compiler emits an
``allowed_tool_unknown_anomalous`` event to ``logs/events.jsonl``.
The ``_anomalous`` suffix puts it in the failure-shaped feedback
bucket (per ``ops_dashboard._FAILURE_SUFFIXES``) so the next turn's
``Recent feedback signals`` block surfaces it to the agent /
operator. Catches frontmatter drift early without manual audit.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)


# Skills that declare ``allowed-tools`` for documentation but need parent
# context to operate. Bypass subagent compilation; they stay inline.
#
# Promote to an ``inline: true`` frontmatter flag in the follow-up that
# generalizes the spike.
_REFLECTIVE_OVERRIDE: frozenset[str] = frozenset({
    "memory",
    "wiki",
})


# Framework built-in tool names (capitalized) that deepagents'
# middleware stack auto-injects into every SubAgent. Listing them in
# ``allowed-tools`` is informative for humans but doesn't require
# explicit registration in the SubAgent ``tools=`` field. Anything
# not in this set is treated as a mimir-custom tool to resolve.
_FRAMEWORK_BUILTINS: frozenset[str] = frozenset({
    "Bash", "Read", "Write", "Edit", "Glob", "Grep",
    "TodoWrite", "Task", "WebFetch", "WebSearch",
    "NotebookEdit", "BashOutput", "KillShell",
})


@dataclass
class CompileResult:
    """Output of :func:`compile_skills_to_subagents`."""

    subagents: list[dict[str, Any]]
    """SubAgent specs ready to pass to ``create_deep_agent(subagents=...)``.

    Each is a plain dict shaped per ``deepagents.SubAgent`` (TypedDict).
    Plain dict instead of the TypedDict subclass so this module doesn't
    have to import from deepagents (keeps test paths cheap)."""

    delegated_skills: set[str]
    """Names of skills that got compiled. Callers (the system-prompt
    assembler) should filter these out of the inline skill catalog so
    the agent sees them only as task-tool subagents — not as
    "read SKILL.md to load"."""

    warnings: list[str]
    """Diagnostic messages from compilation — unknown tool names,
    skills with allowed-tools that resolved to no usable custom tools,
    parse errors per skill. Surfaced for ops visibility, not
    actionable to the agent at runtime."""


def parse_skill_frontmatter(skill_md: Path) -> tuple[dict[str, Any], str]:
    """Return ``(frontmatter_dict, body_text)`` for a SKILL.md file.

    Tolerates: missing frontmatter (returns empty dict + full text as
    body), malformed YAML in the frontmatter block (returns empty
    dict + best-effort body), missing closing ``---`` separator
    (returns empty dict + full text).

    Never raises on file content — only on read failure, which the
    caller is expected to handle.
    """
    raw = skill_md.read_text(encoding="utf-8")
    if not raw.startswith("---"):
        return {}, raw
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return {}, raw
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError as exc:
        log.warning("malformed frontmatter in %s: %s", skill_md, exc)
        return {}, parts[2].lstrip("\n")
    if not isinstance(meta, dict):
        return {}, parts[2].lstrip("\n")
    return meta, parts[2].lstrip("\n")


def _emit_unknown_tool_event(
    *, skill: str, skill_path: str, unknown: list[str], registry_size: int,
) -> None:
    """Emit an ``allowed_tool_unknown_anomalous`` event to events.jsonl.

    The ``_anomalous`` suffix puts this in the failure-shaped feedback
    bucket (per ``ops_dashboard._FAILURE_SUFFIXES``), so the next turn's
    ``Recent feedback signals`` block surfaces the drift. The
    surrounding compile path swallows failures here — the logger may
    not be initialized in test / standalone contexts, and we don't
    want to crash startup over a telemetry write.
    """
    try:
        from .event_logger import log_event_sync
        log_event_sync(
            "allowed_tool_unknown_anomalous",
            skill=skill,
            skill_path=skill_path,
            unknown_tools=unknown,
            registry_size=registry_size,
        )
    except (RuntimeError, OSError, ImportError):
        # logger not initialized, file IO error, or stripped-down test
        # environment — telemetry is best-effort, never load-bearing.
        pass


def _render_params_block(params_schema: dict[str, Any]) -> str:
    """Render a JSON Schema ``params`` dict as a system-prompt block.

    The subagent receives a parent-supplied free-text ``description``
    as its user message; the params block tells the subagent what
    shape that description is expected to carry. Operators author
    using JSON Schema (the framework standard); we render to YAML for
    legibility in the prompt.

    Example output:

    .. code-block:: markdown

        ## Parameters

        The parent agent should supply the following params in the
        task description. Schema:

        ```yaml
        type: object
        properties:
          city:
            type: string
            description: City name (e.g. "Charlotte, NC")
        required: [city]
        ```
    """
    schema_yaml = yaml.safe_dump(
        params_schema, default_flow_style=False, sort_keys=False,
    ).rstrip()
    return (
        "\n\n## Parameters\n\n"
        "The parent agent should supply the following params in the task description. "
        "Schema:\n\n"
        f"```yaml\n{schema_yaml}\n```\n"
    )


def _render_param_summary(params_schema: dict[str, Any]) -> str:
    """One-line param summary suitable for appending to the SubAgent's
    description (which the parent agent sees when deciding to invoke).

    Example: ``Params: city (required), days (optional).``
    """
    props = params_schema.get("properties") or {}
    required = set(params_schema.get("required") or [])
    if not props:
        return ""
    parts: list[str] = []
    for name in props:
        if name in required:
            parts.append(f"{name} (required)")
        else:
            parts.append(f"{name} (optional)")
    return f"Params: {', '.join(parts)}."


def _render_return_summary(returns_schema: dict[str, Any]) -> str:
    """One-line return-shape summary for the SubAgent description.

    Example: ``Returns: forecast, high_temp_c, low_temp_c.``

    Top-level ``properties`` keys only — nested structure is visible
    in the system_prompt's ``response_format`` block but kept out of
    the description (a trigger heuristic, not a full schema). A
    deeply-nested ``returns`` schema (e.g. weather's ``current`` and
    ``forecast`` sub-objects) renders as just the top names.
    """
    props = returns_schema.get("properties") or {}
    if not props:
        return ""
    return f"Returns: {', '.join(props.keys())}."


def _resolve_tool(name: str, registry: dict[str, Any]) -> Any | None:
    """Look up a tool by ``allowed-tools`` entry. Returns the tool
    instance, ``"builtin"`` for framework built-ins (caller skips them),
    or ``None`` for unknown names."""
    if name in _FRAMEWORK_BUILTINS:
        return "builtin"
    return registry.get(name)


def _build_registry(tools: list[Any]) -> dict[str, Any]:
    """Index mimir's tool list by name. Tools may expose their name
    via ``.name`` (langchain BaseTool), ``__name__`` (plain function
    wrapped at call time), or neither (skip)."""
    out: dict[str, Any] = {}
    for t in tools:
        name = getattr(t, "name", None) or getattr(t, "__name__", None)
        if isinstance(name, str):
            out[name] = t
    return out


def compile_skills_to_subagents(
    home: Path,
    parent_tools: list[Any],
    *,
    skills_subdir: str | None = None,
    reflective_override: frozenset[str] = _REFLECTIVE_OVERRIDE,
) -> CompileResult:
    """Scan ``<home>/skills/*/SKILL.md`` and
    ``<home>/.mimir_builtin_skills/*/SKILL.md`` and compile eligible
    skills to SubAgent specs.

    Mirrors SkillsMiddleware's dual-location discovery and its
    last-source-wins shadowing: bundled skills are scanned first, then
    operator-installed skills override same-named entries. A fresh
    deployment with only the bundled refresh in place gets the full
    bundled SubAgent set; operators customize a skill by installing
    same-named content under ``<home>/skills/`` (the operator location
    wins on name collision).

    A skill is eligible iff:

    1. The directory contains a readable SKILL.md
    2. Frontmatter parses (or is empty — empty == ineligible)
    3. Frontmatter declares ``allowed-tools`` as a non-empty list
    4. The skill name is NOT in ``reflective_override``

    For each eligible skill, the function produces a SubAgent spec with:

    - ``name`` = frontmatter ``name`` or the directory name
    - ``description`` = frontmatter ``description`` (operator-supplied
      trigger heuristic — what the parent agent uses to decide whether
      to invoke)
    - ``system_prompt`` = the SKILL.md body (frontmatter stripped)
    - ``tools`` = the resolved subset of ``allowed-tools`` (custom
      mimir tools; framework built-ins auto-included via subagent
      middleware)

    Framework built-ins (Bash, Read, Write, etc.) in ``allowed-tools``
    are noted but not explicitly registered — deepagents' default
    subagent middleware stack provides them. So a skill declaring
    ``allowed-tools: [Bash]`` produces a SubAgent with ``tools=[]``
    and still has Bash available at runtime via the middleware.

    ``skills_subdir`` is a legacy kwarg: when set, only that single
    subdir under ``home`` is scanned (no bundled-dir merge). New
    callers should leave it as ``None`` to get the dual-location
    behavior; the parameter exists for tests that need to isolate a
    specific layout.
    """
    from .skill_defs import (
        BUILTIN_SKILLS_DIR_NAME,
        SKILLS_DIR_NAME,
    )

    # Bundled location listed first so operator entries shadow same-named
    # bundled ones (matches SkillsMiddleware's last-source-wins rule).
    if skills_subdir is None:
        scan_dirs = [
            home / BUILTIN_SKILLS_DIR_NAME,
            home / SKILLS_DIR_NAME,
        ]
    else:
        scan_dirs = [home / skills_subdir]

    warnings: list[str] = []
    scan_dirs = [d for d in scan_dirs if d.is_dir()]
    if not scan_dirs:
        return CompileResult(subagents=[], delegated_skills=set(), warnings=warnings)

    registry = _build_registry(parent_tools)
    # dict preserves insertion order so the last source (operator
    # location) overwrites earlier (bundled location) entries on
    # name collision.
    specs_by_name: dict[str, dict[str, Any]] = {}
    delegated: set[str] = set()
    skill_mds: list[Path] = []
    for d in scan_dirs:
        skill_mds.extend(sorted(d.glob("*/SKILL.md")))

    for skill_md in skill_mds:
        skill_name = skill_md.parent.name
        try:
            meta, body = parse_skill_frontmatter(skill_md)
        except OSError as exc:
            warnings.append(f"{skill_name}: could not read SKILL.md ({exc})")
            log.warning("could not read %s: %s", skill_md, exc)
            continue

        if not isinstance(meta, dict) or not meta:
            continue

        allowed = meta.get("allowed-tools") or meta.get("allowed_tools")
        if not allowed or not isinstance(allowed, list):
            continue

        # Frontmatter name wins; directory name is the fallback.
        name = str(meta.get("name") or skill_name)
        if name in reflective_override:
            log.debug(
                "skill %s in reflective_override; staying inline despite "
                "allowed-tools declaration", name,
            )
            continue

        # Resolve allowed-tools entries.
        custom_tools: list[Any] = []
        builtin_count = 0
        unknown: list[str] = []
        for entry in allowed:
            if not isinstance(entry, str):
                continue
            resolved = _resolve_tool(entry, registry)
            if resolved == "builtin":
                builtin_count += 1
            elif resolved is None:
                unknown.append(entry)
            else:
                custom_tools.append(resolved)

        if unknown:
            warnings.append(
                f"{name}: unknown tool names in allowed-tools "
                f"(dropped, skill will compile with smaller surface): "
                f"{', '.join(unknown)}"
            )
            log.warning(
                "skill %s: dropped unknown tool names from allowed-tools: %s",
                name, unknown,
            )
            _emit_unknown_tool_event(
                skill=name,
                skill_path=str(skill_md),
                unknown=unknown,
                registry_size=len(registry),
            )

        # If a skill has allowed-tools but ALL entries were unknown OR
        # framework built-ins only, that's fine — the subagent still
        # has the framework built-ins via middleware. Don't skip.

        description = str(meta.get("description") or f"{name} skill")
        system_prompt = body

        # Optional ``params`` (JSON Schema) — operator declares what
        # the parent agent should pass via the task description.
        # Rendered as a ## Parameters block in the subagent's prompt
        # so the subagent knows what to expect; summarized into the
        # SubAgent description so the parent sees it before invoking.
        params_schema = meta.get("params")
        if isinstance(params_schema, dict) and params_schema:
            system_prompt += _render_params_block(params_schema)
            summary = _render_param_summary(params_schema)
            if summary:
                description = f"{description} {summary}"

        # Optional ``returns`` (JSON Schema) — operator declares the
        # expected output shape. Translates directly to deepagents'
        # ``response_format=`` field, which accepts JSON schema dicts.
        # The framework enforces the schema on the subagent's last
        # message, so the parent receives structured ``tool_result``
        # content matching this shape.
        returns_schema = meta.get("returns")
        if isinstance(returns_schema, dict) and returns_schema:
            summary = _render_return_summary(returns_schema)
            if summary:
                description = f"{description} {summary}"

        spec: dict[str, Any] = {
            "name": name,
            "description": description,
            "system_prompt": system_prompt,
        }
        if custom_tools:
            spec["tools"] = custom_tools
        if isinstance(returns_schema, dict) and returns_schema:
            spec["response_format"] = returns_schema

        # Dual-location: later-seen entry (operator dir) overwrites
        # the earlier-seen bundled entry. Same last-source-wins
        # semantics as SkillsMiddleware so the rendered ``task``
        # catalog and the inline ``Skills System`` catalog stay
        # consistent on what an operator customization shadows.
        specs_by_name[name] = spec
        delegated.add(name)

    return CompileResult(
        subagents=list(specs_by_name.values()),
        delegated_skills=delegated,
        warnings=warnings,
    )
