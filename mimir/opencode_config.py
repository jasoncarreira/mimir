"""Shared OpenCode model and subscription resolution.

OpenCode remains the owner of provider/plugin configuration.  Mimir only reads
the native config and credential metadata needed to choose the same model for
both coding launchers and to prevent an OAuth route falling back to an ambient
metered key.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re

from .model_registry import DEFAULT_MODEL_SPEC


class OpenCodeConfigError(ValueError):
    """The operator's native OpenCode configuration cannot be used."""


class OpenCodeAuthError(ValueError):
    """The selected OpenCode provider has an unsafe or invalid auth path."""


@dataclass(frozen=True)
class OpenCodeInvocation:
    model: str
    provider: str
    model_source: str
    config_path: Path
    auth_type: str | None
    remove_env: tuple[str, ...] = ()
    pass_env: tuple[str, ...] = ()


def opencode_config_path(
    env: Mapping[str, str] | None = None,
    *,
    override: Path | str | None = None,
) -> Path:
    """Resolve OpenCode's own global JSON/JSONC path without inventing a schema."""
    values = os.environ if env is None else env
    if override is not None:
        return Path(override).expanduser()
    explicit = values.get("OPENCODE_CONFIG", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    config_home = values.get("XDG_CONFIG_HOME", "").strip()
    root = Path(config_home).expanduser() if config_home else _home(values) / ".config"
    directory = root / "opencode"
    jsonc_path = directory / "opencode.jsonc"
    json_path = directory / "opencode.json"
    if jsonc_path.exists() or not json_path.exists():
        return jsonc_path
    return json_path


def opencode_auth_path(env: Mapping[str, str] | None = None) -> Path:
    values = os.environ if env is None else env
    data_home = values.get("XDG_DATA_HOME", "").strip()
    root = Path(data_home).expanduser() if data_home else _home(values) / ".local" / "share"
    return root / "opencode" / "auth.json"


def resolve_opencode_invocation(
    explicit_model: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    config_path: Path | str | None = None,
) -> OpenCodeInvocation:
    """Resolve model precedence and inspect the selected provider's auth metadata.

    Precedence is explicit call argument, OpenCode's native ``model`` setting,
    then the agent's live ``MIMIR_MODEL_SPEC``.  Reading the environment on each
    call is intentional: deployments need the correct fallback even when no
    OpenCode config was seeded on disk.
    """
    values = os.environ if env is None else env
    path = opencode_config_path(values, override=config_path)
    native = _read_object(path, "OpenCode config") if path.exists() else {}
    configured_model = native.get("model")
    if configured_model is not None and (
        not isinstance(configured_model, str) or not configured_model.strip()
    ):
        raise OpenCodeConfigError(f"OpenCode config {path} has a non-string model")

    if explicit_model and explicit_model.strip():
        model = explicit_model.strip()
        source = "explicit"
    elif isinstance(configured_model, str):
        model = configured_model.strip()
        source = "opencode_config"
    else:
        model = _agent_model_to_opencode(
            values.get("MIMIR_MODEL_SPEC", DEFAULT_MODEL_SPEC)
        )
        source = "agent_model"

    provider = _provider_from_model(model)
    auth_type, remove_env = _inspect_auth(provider, values)
    pass_env = tuple(sorted(_native_env_references(native)))
    return OpenCodeInvocation(
        model=model,
        provider=provider,
        model_source=source,
        config_path=path,
        auth_type=auth_type,
        remove_env=remove_env,
        pass_env=pass_env,
    )


def _home(env: Mapping[str, str]) -> Path:
    return Path(env.get("HOME", str(Path.home()))).expanduser()


def _agent_model_to_opencode(model_spec: str) -> str:
    route, separator, model = model_spec.strip().partition(":")
    if not separator or not route or not model:
        raise OpenCodeConfigError(
            "MIMIR_MODEL_SPEC must be provider:model when OpenCode has no model configured"
        )
    provider = {"codex-plus": "openai", "claude-code": "anthropic"}.get(route, route)
    return f"{provider}/{model}"


def _provider_from_model(model: str) -> str:
    provider, separator, name = model.partition("/")
    if not separator or not provider or not name:
        raise OpenCodeConfigError(f"OpenCode model must be provider/model, got {model!r}")
    return provider


def _inspect_auth(
    provider: str,
    env: Mapping[str, str],
) -> tuple[str | None, tuple[str, ...]]:
    path = opencode_auth_path(env)
    try:
        entries = _read_object(path, "OpenCode auth store") if path.exists() else {}
    except OpenCodeConfigError as exc:
        raise OpenCodeAuthError(
            f"OpenCode provider {provider!r} cannot use the invalid auth store {path}"
        ) from exc
    entry = entries.get(provider)
    ambient_name = f"{provider.upper().replace('-', '_')}_API_KEY"

    if entry is None:
        # OpenAI is the dangerous special case: without an OpenCode credential
        # entry its built-in provider silently accepts the metered SDK key.
        if provider == "openai" and env.get("OPENAI_API_KEY", "").strip():
            raise OpenCodeAuthError(
                "OpenCode provider 'openai' has no stored auth; refusing ambient "
                "OPENAI_API_KEY metered fallback. Run `opencode auth login` first."
            )
        return None, ()
    if not isinstance(entry, dict):
        raise OpenCodeAuthError(
            f"OpenCode provider {provider!r} has an invalid auth entry in {path}"
        )

    auth_type = entry.get("type")
    if not isinstance(auth_type, str) or not auth_type.strip():
        raise OpenCodeAuthError(
            f"OpenCode provider {provider!r} has no auth type in {path}"
        )
    auth_type = auth_type.strip()
    required = {
        "oauth": ("access", "refresh"),
        "api": ("key",),
        "wellknown": ("key", "token"),
    }.get(auth_type)
    if required is None:
        usable = any(
            key != "type" and isinstance(value, str) and value.strip()
            for key, value in entry.items()
        )
    else:
        usable = any(isinstance(entry.get(key), str) and entry[key].strip() for key in required)
    if not usable:
        raise OpenCodeAuthError(
            f"OpenCode provider {provider!r} has no usable stored credential in {path}"
        )

    # A stored credential is an operator-selected direct-key path.  A stored
    # OAuth credential is stronger: remove the conventional ambient API key so
    # the actual run cannot silently switch who pays if plugin auth regresses.
    remove = (ambient_name,) if auth_type == "oauth" else ()
    return auth_type, remove


def _read_object(path: Path, label: str) -> dict[str, object]:
    try:
        parsed = json.loads(_strip_jsonc(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OpenCodeConfigError(f"{label} {path} is unreadable or invalid") from exc
    if not isinstance(parsed, dict):
        raise OpenCodeConfigError(f"{label} {path} must contain an object")
    return parsed


_ENV_REFERENCE = re.compile(r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}")


def _native_env_references(value: object) -> set[str]:
    """Collect only env names the native OpenCode config explicitly references."""
    if isinstance(value, str):
        return set(_ENV_REFERENCE.findall(value))
    if isinstance(value, dict):
        found: set[str] = set()
        for nested in value.values():
            found.update(_native_env_references(nested))
        return found
    if isinstance(value, list):
        found = set()
        for nested in value:
            found.update(_native_env_references(nested))
        return found
    return set()


def _strip_jsonc(source: str) -> str:
    """Remove JSONC comments and trailing commas while preserving strings."""
    output: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(source):
        char = source[index]
        next_char = source[index + 1] if index + 1 < len(source) else ""
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue
        if char == "/" and next_char == "/":
            index += 2
            while index < len(source) and source[index] not in "\r\n":
                index += 1
            continue
        if char == "/" and next_char == "*":
            index += 2
            while index + 1 < len(source) and source[index:index + 2] != "*/":
                index += 1
            index += 2
            continue
        output.append(char)
        index += 1

    cleaned = "".join(output)
    output = []
    in_string = False
    escaped = False
    index = 0
    while index < len(cleaned):
        char = cleaned[index]
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue
        if char == ",":
            lookahead = index + 1
            while lookahead < len(cleaned) and cleaned[lookahead].isspace():
                lookahead += 1
            if lookahead < len(cleaned) and cleaned[lookahead] in "}]":
                index += 1
                continue
        output.append(char)
        index += 1
    return "".join(output)
