"""Shared OpenCode model and subscription resolution.

OpenCode remains the owner of provider/plugin configuration.  Mimir only reads
the native config and credential metadata needed to choose the same model for
both coding launchers and to prevent an OAuth route falling back to an ambient
metered key.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re

from .model_registry import DEFAULT_MODEL_SPEC


class OpenCodeConfigError(ValueError):
    """The operator's native OpenCode configuration cannot be used."""


class OpenCodeAuthError(OpenCodeConfigError):
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
    provider_config: object = field(default_factory=dict, repr=False, compare=False)
    auth_entry: object | None = field(default=None, repr=False, compare=False)


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
        model = opencode_model_from_agent_spec(
            values.get("MIMIR_MODEL_SPEC", DEFAULT_MODEL_SPEC)
        )
        source = "agent_model"

    provider = _provider_from_model(model)
    provider_config, env_references = _selected_provider_config(native, provider)
    auth_type, remove_env, auth_entry = _inspect_auth(
        provider, values, env_references=env_references
    )
    pass_env = tuple(sorted(env_references))
    return OpenCodeInvocation(
        model=model,
        provider=provider,
        model_source=source,
        config_path=path,
        auth_type=auth_type,
        remove_env=remove_env,
        pass_env=pass_env,
        provider_config=provider_config,
        auth_entry=auth_entry,
    )


def _home(env: Mapping[str, str]) -> Path:
    return Path(env.get("HOME", str(Path.home()))).expanduser()


def opencode_model_from_agent_spec(model_spec: str) -> str:
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
    *,
    env_references: set[str],
) -> tuple[str | None, tuple[str, ...], object | None]:
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
        ambient_present = bool(env.get(ambient_name, "").strip())
        # OpenAI is the dangerous special case: without an OpenCode credential
        # entry its built-in provider silently accepts the metered SDK key.
        if provider == "openai" and ambient_present:
            raise OpenCodeAuthError(
                "OpenCode provider 'openai' has no stored auth; refusing ambient "
                "OPENAI_API_KEY metered fallback. Run `opencode auth login` first."
            )
        if ambient_present or env_references:
            return None, (), None
        raise OpenCodeAuthError(
            f"OpenCode provider {provider!r} has no credential: no stored auth entry, "
            f"no ambient {ambient_name}, and no {{env:NAME}} reference in its native "
            "provider config. Run `opencode auth login` or configure the provider's "
            "credential source."
        )
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
    return auth_type, remove, json.loads(json.dumps(entry))


def _read_object(path: Path, label: str) -> dict[str, object]:
    try:
        parsed = json.loads(_strip_jsonc(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OpenCodeConfigError(f"{label} {path} is unreadable or invalid") from exc
    if not isinstance(parsed, dict):
        raise OpenCodeConfigError(f"{label} {path} must contain an object")
    return parsed


_ENV_REFERENCE = re.compile(r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}")


_SECRET_KEYS = frozenset({
    "apikey", "key", "token", "accesstoken", "refreshtoken", "secret",
    "password", "authorization", "credential", "credentials", "clientsecret",
})
_ENV_REFERENCE_FULL = re.compile(r"\{env:([A-Za-z_][A-Za-z0-9_]{0,127})\}")


def opencode_worker_documents(
    invocation: OpenCodeInvocation,
) -> tuple[bytes, bytes | None]:
    if _is_github_provider(invocation.provider):
        raise OpenCodeConfigError("GitHub providers are not eligible for worker execution")
    references: set[str] = set()
    _validate_provider_config(
        invocation.provider_config, invocation.provider, references
    )
    config = {
        "model": invocation.model,
        "provider": {invocation.provider: invocation.provider_config},
    }
    config_document = json.dumps(
        config, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if invocation.auth_entry is None:
        return config_document, None
    if invocation.auth_type not in {"api", "oauth", "wellknown"}:
        raise OpenCodeAuthError(
            f"OpenCode provider {invocation.provider!r} has an unsupported worker auth type"
        )
    auth_document = json.dumps(
        {invocation.provider: invocation.auth_entry},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return config_document, auth_document


def _selected_provider_config(
    native: Mapping[str, object], provider: str
) -> tuple[object, set[str]]:
    providers = native.get("provider")
    selected = providers.get(provider, {}) if isinstance(providers, dict) else {}
    if not isinstance(selected, dict):
        raise OpenCodeConfigError(
            f"OpenCode provider {provider!r} has an invalid provider config"
        )
    copied = json.loads(json.dumps(selected))
    return copied, _native_env_references(copied)


def _native_env_references(value: object) -> set[str]:
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


def _validate_provider_config(
    value: object,
    provider: str,
    references: set[str],
    *,
    key: str | None = None,
) -> None:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower()) if key else ""
    sensitive = normalized in _SECRET_KEYS
    if sensitive:
        if isinstance(value, str) and not value:
            return
        if isinstance(value, (dict, list)) and not value:
            return
        full = _ENV_REFERENCE_FULL.fullmatch(value) if isinstance(value, str) else None
        if full is not None:
            references.add(full.group(1))
            return
        if isinstance(value, str) and "{env:" in value:
            raise OpenCodeConfigError(
                f"OpenCode provider {provider!r} has a rejected environment reference"
            )
        raise OpenCodeConfigError(
            f"OpenCode provider {provider!r} has an inline credential in field {key!r}"
        )
    if isinstance(value, dict):
        for nested_key, nested in value.items():
            if _is_github_provider(nested_key):
                raise OpenCodeConfigError(
                    f"OpenCode provider {provider!r} contains a GitHub provider config"
                )
            _validate_provider_config(
                nested,
                provider,
                references,
                key=nested_key,
            )
        return
    if isinstance(value, list):
        for nested in value:
            _validate_provider_config(nested, provider, references, key=key)
        return
    if isinstance(value, str):
        full = _ENV_REFERENCE_FULL.fullmatch(value)
        if "{env:" in value and full is None:
            raise OpenCodeConfigError(
                f"OpenCode provider {provider!r} has a rejected environment reference"
            )
        if full is not None:
            references.add(full.group(1))
        return


def _is_github_provider(provider: str) -> bool:
    normalized = provider.strip().lower()
    return normalized == "github" or normalized.startswith("github-")


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
