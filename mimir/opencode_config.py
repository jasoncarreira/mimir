"""Shared OpenCode model, authentication, and worker projection resolution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re

from .contained_execution import SensitiveMaterialScrubber
from .model_registry import DEFAULT_MODEL_SPEC


_CONFIG_REASON_CODES = frozenset({
    "config_malformed",
    "config_missing",
    "config_oversize",
    "config_provider_selection",
    "config_unreadable",
    "config_unsafe_inline_secret",
})
_AUTH_REASON_CODES = frozenset({
    "auth_invalid",
    "auth_malformed",
    "auth_missing",
    "auth_oversize",
    "auth_unreadable",
})
_MAX_DOCUMENT_BYTES = 1024 * 1024


class OpenCodeConfigError(ValueError):
    def __init__(self, reason_code: str) -> None:
        if reason_code not in _CONFIG_REASON_CODES:
            raise ValueError("invalid OpenCode configuration reason code")
        self.reason_code = reason_code
        super().__init__("OpenCode configuration unavailable")


class OpenCodeAuthError(OpenCodeConfigError):
    def __init__(self, reason_code: str) -> None:
        if reason_code not in _AUTH_REASON_CODES:
            raise ValueError("invalid OpenCode authentication reason code")
        self.reason_code = reason_code
        ValueError.__init__(self, "OpenCode authentication unavailable")


@dataclass(frozen=True)
class OpenCodeInvocation:
    model: str
    provider: str
    model_source: str
    config_path: Path = field(repr=False)
    auth_path: Path = field(repr=False)
    auth_type: str | None
    remove_env: tuple[str, ...] = ()
    pass_env: tuple[str, ...] = ()
    provider_config: object = field(default_factory=dict, repr=False, compare=False)
    auth_entry: object | None = field(default=None, repr=False, compare=False)
    scrubber: SensitiveMaterialScrubber = field(repr=False, compare=False, default_factory=SensitiveMaterialScrubber)


@dataclass(frozen=True)
class OpenCodeWorkerDocuments:
    config_document: bytes = field(repr=False)
    auth_document: bytes | None = field(repr=False)
    scrubber: SensitiveMaterialScrubber = field(repr=False, compare=False)


def opencode_config_path(
    env: Mapping[str, str] | None = None,
    *,
    override: Path | str | None = None,
) -> Path:
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
    values = os.environ if env is None else env
    path = opencode_config_path(values, override=config_path)
    auth_path = opencode_auth_path(values)
    scrubber = SensitiveMaterialScrubber(
        home=_home(values), source_paths=(path, auth_path)
    )
    explicitly_configured = config_path is not None or bool(
        values.get("OPENCODE_CONFIG", "").strip()
    )
    if not path.exists():
        if explicitly_configured:
            raise OpenCodeConfigError("config_missing")
        native: dict[str, object] = {}
    else:
        native, config_source = _read_object(path, kind="config")
        scrubber.add_document(config_source)

    configured_model = native.get("model")
    if configured_model is not None and (
        not isinstance(configured_model, str) or not configured_model.strip()
    ):
        raise OpenCodeConfigError("config_provider_selection")

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
        provider,
        values,
        auth_path=auth_path,
        env_references=env_references,
        scrubber=scrubber,
    )
    return OpenCodeInvocation(
        model=model,
        provider=provider,
        model_source=source,
        config_path=path,
        auth_path=auth_path,
        auth_type=auth_type,
        remove_env=remove_env,
        pass_env=tuple(sorted(env_references)),
        provider_config=provider_config,
        auth_entry=auth_entry,
        scrubber=scrubber,
    )


def _home(env: Mapping[str, str]) -> Path:
    return Path(env.get("HOME", str(Path.home()))).expanduser()


def opencode_model_from_agent_spec(model_spec: str) -> str:
    route, separator, model = model_spec.strip().partition(":")
    if not separator or not route or not model:
        raise OpenCodeConfigError("config_provider_selection")
    provider = {"codex-plus": "openai", "claude-code": "anthropic"}.get(route, route)
    return f"{provider}/{model}"


def _provider_from_model(model: str) -> str:
    provider, separator, name = model.partition("/")
    if not separator or not provider or not name:
        raise OpenCodeConfigError("config_provider_selection")
    return provider


def _inspect_auth(
    provider: str,
    env: Mapping[str, str],
    *,
    auth_path: Path,
    env_references: set[str],
    scrubber: SensitiveMaterialScrubber,
) -> tuple[str | None, tuple[str, ...], object | None]:
    if auth_path.exists():
        entries, auth_source = _read_object(auth_path, kind="auth")
        scrubber.add_document(auth_source)
        _add_sensitive_scalars(
            scrubber, entries, skip_keys=frozenset({"type"})
        )
    else:
        entries = {}
    entry = entries.get(provider)
    ambient_name = f"{provider.upper().replace('-', '_')}_API_KEY"
    ambient_present = bool(env.get(ambient_name, "").strip())

    if entry is None:
        if provider == "openai" and ambient_present:
            raise OpenCodeAuthError("auth_missing")
        if ambient_present or env_references:
            return None, (), None
        raise OpenCodeAuthError("auth_missing")
    if not isinstance(entry, dict):
        raise OpenCodeAuthError("auth_invalid")

    auth_type = entry.get("type")
    if not isinstance(auth_type, str) or not auth_type.strip():
        raise OpenCodeAuthError("auth_invalid")
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
        usable = any(
            isinstance(entry.get(key), str) and bool(entry[key].strip())
            for key in required
        )
    if not usable:
        raise OpenCodeAuthError("auth_invalid")

    copied = json.loads(json.dumps(entry))
    _add_sensitive_scalars(scrubber, copied, skip_keys=frozenset({"type"}))
    remove = (ambient_name,) if auth_type == "oauth" else ()
    return auth_type, remove, copied


def _read_object(path: Path, *, kind: str) -> tuple[dict[str, object], bytes]:
    oversize_code = "config_oversize" if kind == "config" else "auth_oversize"
    malformed_code = "config_malformed" if kind == "config" else "auth_malformed"
    unreadable_code = "config_unreadable" if kind == "config" else "auth_unreadable"
    error_type = OpenCodeConfigError if kind == "config" else OpenCodeAuthError
    try:
        with path.open("rb") as source:
            document = source.read(_MAX_DOCUMENT_BYTES + 1)
    except OSError as exc:
        raise error_type(unreadable_code) from exc
    if len(document) > _MAX_DOCUMENT_BYTES:
        raise error_type(oversize_code)
    try:
        text = document.decode("utf-8")
        parsed = json.loads(_strip_jsonc(text))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise error_type(malformed_code) from exc
    if not isinstance(parsed, dict):
        raise error_type(malformed_code)
    return parsed, document


_ENV_REFERENCE = re.compile(r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}")
_SECRET_KEYS = frozenset({
    "apikey", "key", "token", "accesstoken", "refreshtoken", "secret",
    "password", "authorization", "credential", "credentials", "clientsecret",
})
_ENV_REFERENCE_FULL = re.compile(r"\{env:([A-Za-z_][A-Za-z0-9_]{0,127})\}")


def opencode_worker_documents(
    invocation: OpenCodeInvocation,
    env: Mapping[str, str] | None = None,
) -> OpenCodeWorkerDocuments:
    if _is_github_provider(invocation.provider):
        raise OpenCodeConfigError("config_provider_selection")
    values = os.environ if env is None else env
    references: set[str] = set()
    _validate_provider_config(
        invocation.provider_config, invocation.provider, references
    )
    provider_config = _materialize_env_references(
        invocation.provider_config, values, invocation.scrubber
    )
    config = {
        "model": invocation.model,
        "provider": {invocation.provider: provider_config},
    }
    config_document = _encode_projection(config, auth=False)
    auth_document: bytes | None = None
    if invocation.auth_entry is not None:
        if invocation.auth_type not in {"api", "oauth", "wellknown"}:
            raise OpenCodeAuthError("auth_invalid")
        auth_document = _encode_projection(
            {invocation.provider: invocation.auth_entry}, auth=True
        )
    invocation.scrubber.add_document(config_document)
    if auth_document is not None:
        invocation.scrubber.add_document(auth_document)
    return OpenCodeWorkerDocuments(
        config_document=config_document,
        auth_document=auth_document,
        scrubber=invocation.scrubber,
    )


def _encode_projection(value: object, *, auth: bool) -> bytes:
    document = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(document) > _MAX_DOCUMENT_BYTES:
        if auth:
            raise OpenCodeAuthError("auth_oversize")
        raise OpenCodeConfigError("config_oversize")
    return document


def _materialize_env_references(
    value: object,
    env: Mapping[str, str],
    scrubber: SensitiveMaterialScrubber,
) -> object:
    if isinstance(value, str):
        match = _ENV_REFERENCE_FULL.fullmatch(value)
        if match is None:
            return value
        material = env.get(match.group(1), "")
        if not material:
            raise OpenCodeAuthError("auth_missing")
        scrubber.add_scalar(material)
        return material
    if isinstance(value, dict):
        return {
            key: _materialize_env_references(nested, env, scrubber)
            for key, nested in value.items()
        }
    if isinstance(value, list):
        return [_materialize_env_references(nested, env, scrubber) for nested in value]
    return value


def _selected_provider_config(
    native: Mapping[str, object], provider: str
) -> tuple[object, set[str]]:
    providers = native.get("provider")
    selected = providers.get(provider, {}) if isinstance(providers, dict) else {}
    if not isinstance(selected, dict):
        raise OpenCodeConfigError("config_provider_selection")
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
        raise OpenCodeConfigError("config_unsafe_inline_secret")
    if isinstance(value, dict):
        for nested_key, nested in value.items():
            if _is_github_provider(nested_key):
                raise OpenCodeConfigError("config_unsafe_inline_secret")
            _validate_provider_config(nested, provider, references, key=nested_key)
        return
    if isinstance(value, list):
        for nested in value:
            _validate_provider_config(nested, provider, references, key=key)
        return
    if isinstance(value, str):
        full = _ENV_REFERENCE_FULL.fullmatch(value)
        if "{env:" in value and full is None:
            raise OpenCodeConfigError("config_unsafe_inline_secret")
        if full is not None:
            references.add(full.group(1))


def _add_sensitive_scalars(
    scrubber: SensitiveMaterialScrubber,
    value: object,
    *,
    skip_keys: frozenset[str] = frozenset(),
) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if key not in skip_keys:
                _add_sensitive_scalars(scrubber, nested, skip_keys=skip_keys)
    elif isinstance(value, list):
        for nested in value:
            _add_sensitive_scalars(scrubber, nested, skip_keys=skip_keys)
    elif isinstance(value, str) and value:
        scrubber.add_scalar(value)


def _is_github_provider(provider: str) -> bool:
    normalized = provider.strip().lower()
    return normalized == "github" or normalized.startswith("github-")


def _strip_jsonc(source: str) -> str:
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
