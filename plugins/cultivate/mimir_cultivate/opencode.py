"""OpenCode configuration, worker projection, and proposal artifacts."""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import uuid
from typing import Any

from langchain_core.tools import tool

from mimir.access_control import _MAINTENANCE_GIT_BASE_OVERRIDES, _maintenance_git_filter_overrides
from mimir.model_registry import DEFAULT_MODEL_SPEC


_REDACTION = b"<redacted>"


class SensitiveMaterialScrubber:
    __slots__ = ("_materials",)

    def __init__(
        self, *, home: Path | str | None = None, checkout: Path | str | None = None,
        artifact_root: Path | str | None = None, source_paths: Sequence[Path | str] = (),
    ) -> None:
        self._materials: set[bytes] = set()
        controller_home = home if home is not None else os.environ.get("HOME")
        known_paths: list[Path | str] = list(source_paths)
        if controller_home is not None and str(controller_home):
            home_path = Path(controller_home).expanduser()
            config_root = home_path / ".config"
            data_root = home_path / ".local/share"
            known_paths.extend((
                config_root / "opencode/opencode.json",
                config_root / "opencode/opencode.jsonc",
                data_root / "opencode/auth.json",
            ))
        explicit_config = os.environ.get("OPENCODE_CONFIG")
        if explicit_config:
            known_paths.append(explicit_config)
        for path in (controller_home, checkout, artifact_root, *known_paths):
            if path is not None and str(path):
                self.add_path(path)

    def __repr__(self) -> str:
        return "SensitiveMaterialScrubber()"

    def add_path(self, path: Path | str) -> None:
        lexical = os.path.expanduser(os.fspath(path))
        forms = {lexical, str(Path(lexical).resolve(strict=False))}
        for value in tuple(forms):
            candidate = Path(value)
            if candidate.is_absolute():
                forms.add(candidate.as_uri())
        for value in tuple(forms):
            encoded = json.dumps(value, ensure_ascii=False)
            forms.add(encoded)
            forms.add(encoded[1:-1])
        for value in forms:
            self.add_scalar(value)

    def add_document(self, document: bytes | bytearray | memoryview | str) -> None:
        self._add(document)

    def add_scalar(self, value: bytes | bytearray | memoryview | str) -> None:
        self._add(value)

    def contains_sensitive(self, value: bytes | bytearray | memoryview | str) -> bool:
        payload = self._bytes(value)
        return any(material in payload for material in self._materials)

    def scrub_bytes(self, value: bytes | bytearray | memoryview | str) -> bytes:
        payload = self._bytes(value)
        for material in sorted(self._materials, key=len, reverse=True):
            payload = payload.replace(material, _REDACTION)
        return payload

    def scrub_text(
        self, value: bytes | bytearray | memoryview | str, *, errors: str = "replace",
    ) -> str:
        return self.scrub_bytes(value).decode("utf-8", errors=errors)

    def lookahead_bytes(self) -> int:
        if not self._materials:
            return 0
        return max(len(material) for material in self._materials) - 1

    def safe_truncation_length(self, value: bytes | bytearray | memoryview, limit: int) -> int:
        payload = self._bytes(value)
        cut = max(0, min(limit, len(payload)))
        while True:
            for material in self._materials:
                length = len(material)
                found = payload.find(material, max(0, cut - length + 1), cut + length - 1)
                if found != -1:
                    cut = found
                    break
            else:
                return cut

    def _add(self, value: bytes | bytearray | memoryview | str) -> None:
        payload = self._bytes(value)
        if payload:
            self._materials.add(payload)

    @staticmethod
    def _bytes(value: bytes | bytearray | memoryview | str) -> bytes:
        if isinstance(value, str):
            return value.encode("utf-8")
        return bytes(value)


def _sanitized_git_env() -> dict[str, str]:
    return {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_PAGER": "cat",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/false",
    }


_CONFIG_REASON_CODES = frozenset({
    "config_malformed", "config_missing", "config_oversize",
    "config_provider_selection", "config_unreadable", "config_unsafe_inline_secret",
})
_AUTH_REASON_CODES = frozenset({
    "auth_invalid", "auth_malformed", "auth_missing", "auth_oversize", "auth_unreadable",
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


def _home(env: Mapping[str, str]) -> Path:
    return Path(env.get("HOME", str(Path.home()))).expanduser()


def opencode_config_path(
    env: Mapping[str, str] | None = None, *, override: Path | str | None = None,
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


def resolve_opencode_invocation(
    explicit_model: str | None = None, *, env: Mapping[str, str] | None = None,
    config_path: Path | str | None = None,
) -> OpenCodeInvocation:
    values = os.environ if env is None else env
    path = opencode_config_path(values, override=config_path)
    auth_path = opencode_auth_path(values)
    scrubber = SensitiveMaterialScrubber(home=_home(values), source_paths=(path, auth_path))
    explicitly_configured = config_path is not None or bool(values.get("OPENCODE_CONFIG", "").strip())
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
        model = opencode_model_from_agent_spec(values.get("MIMIR_MODEL_SPEC", DEFAULT_MODEL_SPEC))
        source = "agent_model"

    provider = _provider_from_model(model)
    provider_config, env_references = _selected_provider_config(native, provider)
    auth_type, remove_env, auth_entry = _inspect_auth(
        provider, values, auth_path=auth_path, env_references=env_references, scrubber=scrubber,
    )
    return OpenCodeInvocation(
        model=model, provider=provider, model_source=source, config_path=path,
        auth_path=auth_path, auth_type=auth_type, remove_env=remove_env,
        pass_env=tuple(sorted(env_references)), provider_config=provider_config,
        auth_entry=auth_entry, scrubber=scrubber,
    )


def _inspect_auth(
    provider: str, env: Mapping[str, str], *, auth_path: Path,
    env_references: set[str], scrubber: SensitiveMaterialScrubber,
) -> tuple[str | None, tuple[str, ...], object | None]:
    if auth_path.exists():
        entries, auth_source = _read_object(auth_path, kind="auth")
        scrubber.add_document(auth_source)
        _add_sensitive_scalars(scrubber, entries, skip_keys=frozenset({"type"}))
    else:
        entries = {}
    entry = entries.get(provider)
    ambient_name = f"{provider.upper().replace('-', '_')}_API_KEY"
    ambient_present = bool(env.get(ambient_name, "").strip())

    if entry is None:
        if provider == "openai" and ambient_present:
            raise OpenCodeAuthError("auth_missing")
        if ambient_present:
            credential = env[ambient_name].strip()
            scrubber.add_scalar(credential)
            return "api", (ambient_name,), {"type": "api", "key": credential}
        if env_references:
            return None, (), None
        raise OpenCodeAuthError("auth_missing")
    if not isinstance(entry, dict):
        raise OpenCodeAuthError("auth_invalid")
    auth_type = entry.get("type")
    if not isinstance(auth_type, str) or not auth_type.strip():
        raise OpenCodeAuthError("auth_invalid")
    auth_type = auth_type.strip()
    required = {
        "oauth": ("access", "refresh"), "api": ("key",), "wellknown": ("key", "token"),
    }.get(auth_type)
    if required is None:
        usable = any(
            key != "type" and isinstance(value, str) and value.strip()
            for key, value in entry.items()
        )
    else:
        usable = any(isinstance(entry.get(key), str) and bool(entry[key].strip()) for key in required)
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
        parsed = json.loads(_strip_jsonc(document.decode("utf-8")))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise error_type(malformed_code) from exc
    if not isinstance(parsed, dict):
        raise error_type(malformed_code)
    return parsed, document


_ENV_REFERENCE = re.compile(r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}")
_ENV_REFERENCE_FULL = re.compile(r"\{env:([A-Za-z_][A-Za-z0-9_]{0,127})\}")
_SECRET_KEYS = frozenset({
    "apikey", "key", "token", "accesstoken", "refreshtoken", "secret",
    "password", "authorization", "credential", "credentials", "clientsecret",
})


def opencode_worker_documents(
    invocation: OpenCodeInvocation, env: Mapping[str, str] | None = None,
) -> OpenCodeWorkerDocuments:
    if _is_github_provider(invocation.provider):
        raise OpenCodeConfigError("config_provider_selection")
    values = os.environ if env is None else env
    references: set[str] = set()
    _validate_provider_config(invocation.provider_config, invocation.provider, references)
    provider_config = _materialize_env_references(invocation.provider_config, values, invocation.scrubber)
    config = {
        "model": invocation.model,
        "provider": {invocation.provider: provider_config},
        "permission": {"external_directory": "deny"},
    }
    config_document = _encode_projection(config, auth=False)
    auth_document: bytes | None = None
    if invocation.auth_entry is not None:
        if invocation.auth_type not in {"api", "oauth", "wellknown"}:
            raise OpenCodeAuthError("auth_invalid")
        auth_document = _encode_projection({invocation.provider: invocation.auth_entry}, auth=True)
    invocation.scrubber.add_document(config_document)
    if auth_document is not None:
        invocation.scrubber.add_document(auth_document)
    return OpenCodeWorkerDocuments(config_document, auth_document, invocation.scrubber)


def _encode_projection(value: object, *, auth: bool) -> bytes:
    document = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(document) > _MAX_DOCUMENT_BYTES:
        if auth:
            raise OpenCodeAuthError("auth_oversize")
        raise OpenCodeConfigError("config_oversize")
    return document


def _materialize_env_references(
    value: object, env: Mapping[str, str], scrubber: SensitiveMaterialScrubber,
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
        return {key: _materialize_env_references(nested, env, scrubber) for key, nested in value.items()}
    if isinstance(value, list):
        return [_materialize_env_references(nested, env, scrubber) for nested in value]
    return value


def _selected_provider_config(native: Mapping[str, object], provider: str) -> tuple[object, set[str]]:
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
    value: object, provider: str, references: set[str], *, key: str | None = None,
) -> None:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower()) if key else ""
    if normalized in _SECRET_KEYS:
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
    scrubber: SensitiveMaterialScrubber, value: object,
    *, skip_keys: frozenset[str] = frozenset(),
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


MAX_PROPOSAL_PATHS = 1_000
MAX_PROPOSAL_PATH_BYTES = 4_096
MAX_PROPOSAL_NAME_STREAM_BYTES = 4_097_000
MAX_PROPOSAL_PATCH_BYTES = 1_048_576
_SAFE_HANDLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_PATH_TOKEN = re.compile(r"(?:file://)?/[^\s\"'<>]+")


@dataclass(frozen=True)
class OpenCodeProposal:
    schema_version: int
    kind: str
    base_tree: str
    encoding: str
    byte_length: int
    sha256: str
    patch: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProposalBuildResult:
    proposal: OpenCodeProposal | None
    reason_code: str


@dataclass(frozen=True)
class SpawnTerminalResult:
    status: str
    exit_code: int | None
    reason_code: str
    event_type: str
    proposal: OpenCodeProposal | None = None

    def event(self, run_id: str) -> dict[str, str]:
        return {
            "type": self.event_type, "run_id": run_id,
            "status": self.status, "reason_code": self.reason_code,
        }


def _path_forms(path: Path | str) -> set[str]:
    lexical = os.path.expanduser(os.fspath(path))
    resolved = str(Path(lexical).resolve(strict=False))
    forms = {lexical, resolved}
    for value in tuple(forms):
        candidate = Path(value)
        if candidate.is_absolute():
            forms.add(candidate.as_uri())
    for value in tuple(forms):
        encoded = json.dumps(value, ensure_ascii=False)
        forms.add(encoded)
        forms.add(encoded[1:-1])
    return {value for value in forms if value}


def _beneath(candidate: str, home: Path) -> bool:
    try:
        lexical = Path(os.path.normpath(candidate))
        resolved = lexical.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return False
    for value in (lexical, resolved):
        try:
            value.relative_to(home)
            return True
        except ValueError:
            pass
    return False


def _contains_path_form(prompt: str, form: str, *, descendants: bool) -> bool:
    start = 0
    while (index := prompt.find(form, start)) >= 0:
        before = prompt[index - 1] if index else ""
        end = index + len(form)
        after = prompt[end] if end < len(prompt) else ""
        before_boundary = not before or before.isspace() or before in "\"'([{<"
        after_boundary = not after or after.isspace() or after in "\"')]}>,;:!?"
        if before_boundary and (after_boundary or (descendants and after == "/")):
            return True
        start = index + 1
    return False


def prompt_contains_sensitive_source_path(
    prompt: str, *, agent_home: Path | str, source_paths: Sequence[Path | str] = (),
) -> str | None:
    if not isinstance(prompt, str):
        raise TypeError("prompt must be a string")
    home = Path(os.path.expanduser(os.fspath(agent_home))).resolve(strict=False)
    if any(
        _contains_path_form(prompt, form, descendants=True)
        for form in _path_forms(agent_home)
    ):
        return "agent_home_path"
    for match in _PATH_TOKEN.finditer(prompt):
        token = match.group(0).rstrip(".,;:!?)]}")
        if token.startswith("file://"):
            token = token[7:]
        if _beneath(token, home):
            return "agent_home_path"
    tokens = {match.group(0).rstrip(".,;:!?)]}") for match in _PATH_TOKEN.finditer(prompt)}
    for source_path in source_paths:
        forms = _path_forms(source_path)
        if tokens.intersection(forms):
            return "config_auth_source_path"
        if any(_contains_path_form(prompt, form, descendants=False) for form in forms):
            return "config_auth_source_path"
    return None


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _run_git(checkout: Path, *args: str, limit: int | None = None) -> bytes:
    env = _sanitized_git_env()
    filters = _maintenance_git_filter_overrides(checkout, "git", effective_config=True, env=env)
    if filters is None:
        raise OSError("proposal Git filter configuration unavailable")
    command = ("git", "-C", os.fspath(checkout), *_MAINTENANCE_GIT_BASE_OVERRIDES, *filters, *args)
    if limit is None:
        completed = subprocess.run(
            command, check=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
        )
        if completed.returncode != 0:
            raise OSError("proposal Git operation failed")
        return completed.stdout
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
    if process.stdout is None:
        _stop_process(process)
        raise OSError("proposal Git output unavailable")
    output = process.stdout.read(limit)
    if len(output) == limit:
        process.stdout.close()
        _stop_process(process)
        return output
    return_code = process.wait()
    process.stdout.close()
    if return_code != 0:
        raise OSError("proposal Git operation failed")
    return output


def build_opencode_proposal(
    checkout: Path | str, *, scrubber: SensitiveMaterialScrubber, base_tree: str | None = None,
) -> ProposalBuildResult:
    root = Path(checkout)
    try:
        _run_git(root, "add", "-A")
        name_stream = _run_git(
            root, "diff", "--cached", "--name-only", "-z", "--no-ext-diff", "--no-textconv",
            limit=MAX_PROPOSAL_NAME_STREAM_BYTES + 1,
        )
        if len(name_stream) > MAX_PROPOSAL_NAME_STREAM_BYTES:
            return ProposalBuildResult(None, "path_bytes")
        if not name_stream:
            return ProposalBuildResult(None, "no_changes")
        paths = name_stream[:-1].split(b"\0") if name_stream.endswith(b"\0") else []
        if not paths or any(not path for path in paths):
            return ProposalBuildResult(None, "proposal_unavailable")
        if len(paths) > MAX_PROPOSAL_PATHS:
            return ProposalBuildResult(None, "path_count")
        for path in paths:
            if len(path) > MAX_PROPOSAL_PATH_BYTES:
                return ProposalBuildResult(None, "path_bytes")
            pure = PurePosixPath(os.fsdecode(path))
            if pure.is_absolute() or ".." in pure.parts:
                return ProposalBuildResult(None, "proposal_unavailable")
        patch = _run_git(
            root, "diff", "--cached", "--binary", "--full-index", "--no-ext-diff", "--no-textconv",
            limit=MAX_PROPOSAL_PATCH_BYTES + 1,
        )
        if len(patch) > MAX_PROPOSAL_PATCH_BYTES:
            return ProposalBuildResult(None, "patch_bytes")
        if scrubber.contains_sensitive(patch):
            return ProposalBuildResult(None, "proposal_sensitive_content")
        tree = base_tree or _run_git(root, "rev-parse", "HEAD^{tree}").decode("ascii").strip()
        if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", tree) is None:
            return ProposalBuildResult(None, "proposal_unavailable")
    except (OSError, UnicodeError, ValueError):
        return ProposalBuildResult(None, "proposal_unavailable")
    proposal = OpenCodeProposal(
        schema_version=1, kind="git_binary_patch", base_tree=tree.lower(), encoding="base64",
        byte_length=len(patch), sha256=hashlib.sha256(patch).hexdigest(),
        patch=base64.b64encode(patch).decode("ascii"),
    )
    return ProposalBuildResult(proposal, "proposal_created")


def resolve_artifact_handle(artifact_root: Path | str, handle: str) -> Path:
    if not isinstance(handle, str) or _SAFE_HANDLE.fullmatch(handle) is None:
        raise ValueError("unsafe artifact handle")
    if handle in {".", ".."}:
        raise ValueError("unsafe artifact handle")
    root = Path(artifact_root).resolve(strict=False)
    candidate = (root / handle).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("unsafe artifact handle") from exc
    return candidate


def write_spawn_artifacts(
    artifact_root: Path | str, handle: str, *, prompt: str, stdout: bytes | str,
    stderr: bytes | str, manifest: Mapping[str, Any], scrubber: SensitiveMaterialScrubber,
    proposal: OpenCodeProposal | None = None,
) -> str:
    directory = resolve_artifact_handle(artifact_root, handle)
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    prompt_text = scrubber.scrub_text(prompt)
    stdout_text = scrubber.scrub_text(stdout)
    stderr_text = scrubber.scrub_text(stderr)
    scrubbed_manifest = json.loads(scrubber.scrub_text(json.dumps(dict(manifest), ensure_ascii=False)))
    (directory / "prompt.md").write_text(prompt_text, encoding="utf-8")
    (directory / "stdout.txt").write_text(stdout_text, encoding="utf-8")
    (directory / "stderr.txt").write_text(stderr_text, encoding="utf-8")
    (directory / "manifest.json").write_text(
        json.dumps(scrubbed_manifest, ensure_ascii=True, sort_keys=True) + "\n", encoding="utf-8",
    )
    if proposal is not None:
        raw_patch = base64.b64decode(proposal.patch, validate=True)
        if scrubber.contains_sensitive(raw_patch):
            raise ValueError("proposal contains sensitive material")
        (directory / "proposal.json").write_text(
            json.dumps(proposal.as_dict(), ensure_ascii=True, sort_keys=True) + "\n", encoding="utf-8",
        )
    return handle


def classify_spawn_terminal_state(
    *, artifact_available: bool = True, configuration_reason: str | None = None,
    authentication_reason: str | None = None, prompt_reason: str | None = None,
    provisioning_reason: str | None = None, containment_available: bool = True,
    timed_out: bool = False, output_overflow: bool = False, exit_code: int | None = None,
    worker_authentication_required: bool = False,
    proposal_result: ProposalBuildResult | None = None,
) -> SpawnTerminalResult:
    if not artifact_available:
        return SpawnTerminalResult("artifact_unavailable", None, "artifact_unavailable", "spawn_open_code_provisioning_refused")
    if configuration_reason is not None:
        return SpawnTerminalResult("configuration_refused", None, configuration_reason, "spawn_open_code_configuration_refused")
    if authentication_reason is not None:
        return SpawnTerminalResult("authentication_refused", None, authentication_reason, "spawn_open_code_authentication_refused")
    if prompt_reason is not None:
        return SpawnTerminalResult("prompt_refused", None, prompt_reason, "spawn_open_code_prompt_refused")
    if provisioning_reason is not None:
        status_by_reason = {
            "seed_credentials": "seed_credentials_refused",
            "invalid_seed": "seed_refused", "unsafe_seed_entry": "seed_refused",
            "embedded_seed_repository": "seed_refused",
            "seed_changed": "seed_refused", "provisioning_unavailable": "provisioning_unavailable",
        }
        status = status_by_reason.get(provisioning_reason, "provisioning_unavailable")
        return SpawnTerminalResult(status, None, provisioning_reason, "spawn_open_code_provisioning_refused")
    if not containment_available:
        return SpawnTerminalResult("containment_unavailable", None, "containment_unavailable", "spawn_open_code_containment_refused")
    if timed_out:
        return SpawnTerminalResult("timeout", None, "timeout", "spawn_open_code_completed")
    if output_overflow:
        return SpawnTerminalResult("output_overflow", exit_code, "output_overflow", "spawn_open_code_completed")
    if exit_code != 0:
        if worker_authentication_required:
            return SpawnTerminalResult("authentication_required", exit_code, "worker_authentication_required", "spawn_open_code_completed")
        return SpawnTerminalResult("failed", exit_code, "worker_nonzero", "spawn_open_code_completed")
    if proposal_result is None:
        return SpawnTerminalResult("proposal_unavailable", 0, "proposal_unavailable", "spawn_open_code_completed")
    reason = proposal_result.reason_code
    if reason == "no_changes":
        return SpawnTerminalResult("succeeded", 0, reason, "spawn_open_code_completed")
    if reason in {"path_count", "path_bytes", "patch_bytes"}:
        return SpawnTerminalResult("proposal_overflow", 0, reason, "spawn_open_code_completed")
    if reason == "proposal_sensitive_content":
        return SpawnTerminalResult(reason, 0, reason, "spawn_open_code_completed")
    if reason != "proposal_created" or proposal_result.proposal is None:
        return SpawnTerminalResult("proposal_unavailable", 0, "proposal_unavailable", "spawn_open_code_completed")
    return SpawnTerminalResult("succeeded", 0, reason, "spawn_open_code_completed", proposal_result.proposal)


def cleanup_failure_event(run_id: str) -> dict[str, str]:
    return {"type": "spawn_open_code_cleanup_failed", "run_id": run_id, "reason_code": "cleanup_failed"}


@tool
async def spawn_open_code(
    prompt: str,
    cwd: str | None = None,
    timeout_s: int = 1800,
    name: str | None = None,
    model: str | None = None,
    agent: str | None = None,
    artifact_root: str | None = None,
) -> str:
    """Run OpenCode in a fresh contained checkout and return a bounded proposal."""
    from mimir.tools.registry import _spawn_open_code_impl

    from .contained_checkout import create_opencode_checkout
    from .contained_execution import execute_contained

    return await _spawn_open_code_impl(
        prompt, cwd, timeout_s, name, model, agent, artifact_root,
        contained_runner=execute_contained,
        checkout_factory=create_opencode_checkout,
        execution_identifier=str(uuid.uuid4()),
    )
