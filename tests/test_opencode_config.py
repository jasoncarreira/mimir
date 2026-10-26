from __future__ import annotations

import json
import base64
from pathlib import Path

import pytest

pytest.importorskip("mimir_cultivate", reason="OpenCode configuration belongs to Cultivate")

from mimir_cultivate.opencode import (
    OpenCodeAuthError,
    OpenCodeConfigError,
    opencode_config_path,
    opencode_worker_documents,
    reconcile_codex_auth,
    resolve_opencode_invocation,
    SensitiveMaterialScrubber,
    _inspect_auth,
)


def _env(tmp_path: Path, **values: str) -> dict[str, str]:
    return {"HOME": str(tmp_path), **values}


def _write_auth(tmp_path: Path, payload: object) -> None:
    path = tmp_path / ".local" / "share" / "opencode" / "auth.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _jwt(exp: int) -> str:
    segment = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return ".".join(("header", segment, "signature"))


def _codex(tmp_path: Path, exp: int = 300) -> dict[str, object]:
    document = {"auth_mode": "chatgpt", "tokens": {
        "access_token": _jwt(exp), "refresh_token": "refresh-" + str(exp),
        "id_token": "kept", "account_id": "account",
    }, "operator_field": "preserved"}
    path = tmp_path / ".codex" / "auth.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document))
    path.chmod(0o600)
    return document


def test_native_jsonc_model_wins_over_live_agent_model(tmp_path: Path) -> None:
    config = tmp_path / ".config" / "opencode" / "opencode.jsonc"
    config.parent.mkdir(parents=True)
    config.write_text(
        '{\n // operator-owned provider config\n "model": "zai/glm-5",\n'
        ' "provider": {"zai": {"options": {"apiKey": "{env:ZAI_TOKEN}"}}},\n}\n',
        encoding="utf-8",
    )
    _write_auth(tmp_path, {"zai": {"type": "api", "key": "secret-not-reported"}})

    resolved = resolve_opencode_invocation(
        env=_env(tmp_path, MIMIR_MODEL_SPEC="codex-plus:gpt-agent")
    )

    assert resolved.model == "zai/glm-5"
    assert resolved.provider == "zai"
    assert resolved.model_source == "opencode_config"
    assert resolved.auth_type == "api"
    assert resolved.pass_env == ("ZAI_TOKEN",)
    assert "secret-not-reported" not in repr(resolved)


def test_explicit_model_wins_over_native_config(tmp_path: Path) -> None:
    config = tmp_path / "custom.jsonc"
    config.write_text('{"model":"openrouter/configured"}', encoding="utf-8")
    resolved = resolve_opencode_invocation(
        "opencode/explicit",
        env=_env(
            tmp_path,
            OPENCODE_CONFIG=str(config),
            OPENCODE_API_KEY="configured-elsewhere",
        ),
    )
    assert resolved.model == "opencode/explicit"
    assert resolved.model_source == "explicit"


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("openai:gpt-5.6-luna", "openai/gpt-5.6-luna"),
        ("claude-code:claude-sonnet-4-6", "anthropic/claude-sonnet-4-6"),
        ("openrouter:moonshotai/kimi-k2", "openrouter/moonshotai/kimi-k2"),
    ],
)
def test_missing_config_falls_back_to_live_agent_model(
    tmp_path: Path, spec: str, expected: str
) -> None:
    provider = expected.partition("/")[0]
    _write_auth(tmp_path, {provider: {"type": "api", "key": "test-credential"}})
    resolved = resolve_opencode_invocation(env=_env(tmp_path, MIMIR_MODEL_SPEC=spec))
    assert resolved.model == expected
    assert resolved.model_source == "agent_model"


def test_openai_oauth_is_distinguished_and_disables_metered_fallback(
    tmp_path: Path,
) -> None:
    _codex(tmp_path)
    resolved = resolve_opencode_invocation(
        env=_env(
            tmp_path,
            MIMIR_MODEL_SPEC="codex-plus:gpt-5.6-luna",
            OPENAI_API_KEY="metered-secret",
        )
    )
    assert resolved.auth_type == "oauth"
    assert resolved.remove_env == ("OPENAI_API_KEY",)
    assert "refresh-300" not in repr(resolved)
    assert "metered-secret" not in repr(resolved)


def test_openai_ambient_key_without_stored_auth_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(OpenCodeAuthError) as isolated:
        _inspect_auth(
            "openai", _env(tmp_path, OPENAI_API_KEY="metered-key"),
            auth_path=tmp_path / "absent.json", env_references=set(),
            scrubber=SensitiveMaterialScrubber(home=tmp_path), codex=True,
        )
    assert isolated.value.reason_code == "auth_missing"
    with pytest.raises(OpenCodeAuthError, match="OpenCode authentication unavailable") as raised:
        resolve_opencode_invocation(
            env=_env(
                tmp_path,
                MIMIR_MODEL_SPEC="codex-plus:gpt-5.6-luna",
                OPENAI_API_KEY="must-not-appear",
            )
        )
    assert raised.value.reason_code == "auth_missing"


def test_missing_provider_credential_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(OpenCodeAuthError, match="OpenCode authentication unavailable"):
        resolve_opencode_invocation(
            env=_env(tmp_path, MIMIR_MODEL_SPEC="claude-code:claude-sonnet-4-6")
        )


def test_selected_provider_env_reference_is_accepted(tmp_path: Path) -> None:
    config = tmp_path / ".config" / "opencode" / "opencode.jsonc"
    config.parent.mkdir(parents=True)
    config.write_text(
        '{"model":"proxy/model","provider":{'
        '"proxy":{"options":{"apiKey":"{env:PROXY_TOKEN}"}},'
        '"other":{"options":{"apiKey":"{env:OTHER_TOKEN}"}}}}',
        encoding="utf-8",
    )

    resolved = resolve_opencode_invocation(env=_env(tmp_path))

    assert resolved.provider == "proxy"
    assert resolved.auth_type is None
    assert resolved.pass_env == ("PROXY_TOKEN",)


def test_unselected_provider_env_reference_does_not_hide_missing_auth(
    tmp_path: Path,
) -> None:
    config = tmp_path / ".config" / "opencode" / "opencode.jsonc"
    config.parent.mkdir(parents=True)
    config.write_text(
        '{"model":"anthropic/claude","provider":{'
        '"proxy":{"options":{"apiKey":"{env:PROXY_TOKEN}"}}}}',
        encoding="utf-8",
    )

    with pytest.raises(OpenCodeAuthError, match="OpenCode authentication unavailable"):
        resolve_opencode_invocation(env=_env(tmp_path))


def test_arbitrary_provider_auth_type_is_not_hardcoded(tmp_path: Path) -> None:
    _write_auth(tmp_path, {"future-provider": {"type": "device-flow-v2", "token": "secret"}})
    resolved = resolve_opencode_invocation(
        env=_env(tmp_path, MIMIR_MODEL_SPEC="future-provider:model")
    )
    assert resolved.provider == "future-provider"
    assert resolved.auth_type == "device-flow-v2"


def test_invalid_config_fails_without_leaking_contents(tmp_path: Path) -> None:
    config = tmp_path / "broken.jsonc"
    config.write_text('{"model": "openai/secret-model", broken}', encoding="utf-8")
    with pytest.raises(OpenCodeConfigError) as raised:
        resolve_opencode_invocation(env=_env(tmp_path, OPENCODE_CONFIG=str(config)))
    assert "secret-model" not in str(raised.value)


def test_config_path_uses_native_environment_override(tmp_path: Path) -> None:
    custom = tmp_path / "operator" / "opencode.jsonc"
    assert opencode_config_path(_env(tmp_path, OPENCODE_CONFIG=str(custom))) == custom


def test_call_time_agent_model_loads_home_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from mimir.config import model_spec_at_call_time

    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.delenv("MIMIR_MODEL_SPEC", raising=False)
    (tmp_path / ".env").write_text(
        "MIMIR_MODEL_SPEC=openrouter:configured-at-call-time\n",
        encoding="utf-8",
    )

    assert model_spec_at_call_time() == "openrouter:configured-at-call-time"


@pytest.mark.parametrize(
    "entry",
    [
        {"type": "api", "key": "selected-api-literal", "metadata": {"account": "a"}},
        {"type": "oauth", "access": "selected-access", "refresh": "selected-refresh"},
        {"type": "wellknown", "key": "selected-key", "token": "selected-token"},
    ],
)
def test_worker_documents_select_only_complete_native_record(
    tmp_path: Path, entry: dict[str, object]
) -> None:
    config = tmp_path / ".config" / "opencode" / "opencode.jsonc"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps({
            "model": "proxy/model",
            "plugin": ["inactive-plugin"],
            "provider": {
                "proxy": {"options": {"apiKey": "{env:PROXY_TOKEN}"}},
                "inactive": {"options": {"key": "inactive-config-secret"}},
                "github-copilot": {"options": {"token": "github-config-secret"}},
            },
        }),
        encoding="utf-8",
    )
    _write_auth(tmp_path, {
        "proxy": entry,
        "inactive": {"type": "api", "key": "inactive-auth-secret"},
        "github": {"type": "api", "key": "github-auth-secret"},
    })

    invocation = resolve_opencode_invocation(env=_env(tmp_path, PROXY_TOKEN="from-env"))
    documents = opencode_worker_documents(invocation, _env(tmp_path, PROXY_TOKEN="from-env"))
    config_document = documents.config_document
    auth_document = documents.auth_document

    assert json.loads(config_document) == {
        "model": "proxy/model",
        "provider": {"proxy": {"options": {"apiKey": "from-env"}}},
        # Worker documents deny external directories on every surface that
        # builds them; asserted here so the containment default cannot be
        # dropped silently by a later config edit.
        "permission": {"external_directory": "deny"},
    }
    assert auth_document is not None
    assert json.loads(auth_document) == {"proxy": entry}
    combined = config_document + auth_document
    assert b"inactive" not in combined
    assert b"github" not in combined.lower()


def test_inline_provider_secret_rejects_without_value_leak(tmp_path: Path) -> None:
    secret = "inline-sentinel-must-not-leak"
    config = tmp_path / "opencode.json"
    config.write_text(
        json.dumps({
            "model": "proxy/model",
            "provider": {"proxy": {"options": {"client_secret": secret}}},
        }),
        encoding="utf-8",
    )

    invocation = resolve_opencode_invocation(
        env=_env(tmp_path, OPENCODE_CONFIG=str(config), PROXY_API_KEY="ambient")
    )
    with pytest.raises(OpenCodeConfigError) as raised:
        opencode_worker_documents(invocation)

    assert secret not in str(raised.value)


@pytest.mark.parametrize(
    "provider_config",
    [
        {"apiKey": {"nested": "nested-secret-must-not-leak"}},
        {"token": ["listed-secret-must-not-leak"]},
        {"password": None},
        {"credentials": 1},
        {"authorization": False},
    ],
)
def test_sensitive_provider_config_rejects_non_reference_values_without_leaking(
    tmp_path: Path, provider_config: dict[str, object]
) -> None:
    config = tmp_path / "opencode.json"
    config.write_text(
        json.dumps({"model": "proxy/model", "provider": {"proxy": provider_config}}),
        encoding="utf-8",
    )
    invocation = resolve_opencode_invocation(
        env=_env(tmp_path, OPENCODE_CONFIG=str(config), PROXY_API_KEY="ambient")
    )

    with pytest.raises(OpenCodeConfigError) as raised:
        opencode_worker_documents(invocation)

    message = str(raised.value)
    assert "nested-secret-must-not-leak" not in message
    assert "listed-secret-must-not-leak" not in message


@pytest.mark.parametrize(
    "provider_config",
    [
        {"clientSecret": ""},
        {"credentials": {}},
        {"authorization": []},
    ],
)
def test_sensitive_provider_config_permits_empty_values(
    tmp_path: Path, provider_config: dict[str, object]
) -> None:
    config = tmp_path / "opencode.json"
    config.write_text(
        json.dumps({"model": "proxy/model", "provider": {"proxy": provider_config}}),
        encoding="utf-8",
    )
    invocation = resolve_opencode_invocation(
        env=_env(tmp_path, OPENCODE_CONFIG=str(config), PROXY_API_KEY="ambient")
    )

    documents = opencode_worker_documents(invocation)

    assert json.loads(documents.config_document)["provider"]["proxy"] == provider_config
    auth_document = documents.auth_document
    assert auth_document is not None
    assert json.loads(auth_document) == {
        "proxy": {"type": "api", "key": "ambient"}
    }


@pytest.mark.parametrize(
    "provider_config",
    [
        {"credentials": {"nested": "{env:PROXY_TOKEN}"}},
        {"authorization": ["{env:PROXY_TOKEN}"]},
    ],
)
def test_sensitive_provider_config_rejects_nested_exact_env_references(
    tmp_path: Path, provider_config: dict[str, object]
) -> None:
    config = tmp_path / "opencode.json"
    config.write_text(
        json.dumps({"model": "proxy/model", "provider": {"proxy": provider_config}}),
        encoding="utf-8",
    )
    invocation = resolve_opencode_invocation(
        env=_env(tmp_path, OPENCODE_CONFIG=str(config), PROXY_TOKEN="referenced")
    )

    with pytest.raises(OpenCodeConfigError) as raised:
        opencode_worker_documents(invocation)

    assert "referenced" not in str(raised.value)


@pytest.mark.parametrize(
    "provider_config",
    [
        {"apiKey": "prefix-{env:PROXY_TOKEN}"},
        {"github": {"apiKey": "{env:PROXY_TOKEN}"}},
        {"github-copilot": {}},
    ],
)
def test_worker_provider_config_rejects_unsafe_references_and_github_keys(
    tmp_path: Path, provider_config: dict[str, object]
) -> None:
    config = tmp_path / "opencode.json"
    config.write_text(
        json.dumps({"model": "proxy/model", "provider": {"proxy": provider_config}}),
        encoding="utf-8",
    )
    invocation = resolve_opencode_invocation(
        env=_env(tmp_path, OPENCODE_CONFIG=str(config), PROXY_API_KEY="ambient")
    )
    with pytest.raises(OpenCodeConfigError):
        opencode_worker_documents(invocation)


def test_selected_github_provider_is_rejected(tmp_path: Path) -> None:
    config = tmp_path / "opencode.json"
    config.write_text('{"model":"github-copilot/model"}', encoding="utf-8")
    _write_auth(tmp_path, {"github-copilot": {"type": "api", "key": "stored"}})
    invocation = resolve_opencode_invocation(
        env=_env(tmp_path, OPENCODE_CONFIG=str(config))
    )
    with pytest.raises(OpenCodeConfigError, match="OpenCode configuration unavailable"):
        opencode_worker_documents(invocation)


def test_explicit_missing_config_has_stable_failure(tmp_path: Path) -> None:
    missing = tmp_path / "secret-config-name.json"

    with pytest.raises(OpenCodeConfigError) as raised:
        resolve_opencode_invocation(
            env=_env(tmp_path, OPENCODE_CONFIG=str(missing))
        )

    assert raised.value.reason_code == "config_missing"
    assert str(raised.value) == "OpenCode configuration unavailable"
    assert str(missing) not in str(raised.value)


def test_auth_parse_failure_has_stable_failure(tmp_path: Path) -> None:
    _codex(tmp_path)
    auth = tmp_path / ".local" / "share" / "opencode" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text('{"openai":{"type":"oauth","refresh":"secret"},broken}', encoding="utf-8")

    with pytest.raises(OpenCodeAuthError) as raised:
        resolve_opencode_invocation(
            env=_env(tmp_path, MIMIR_MODEL_SPEC="codex-plus:model")
        )

    assert raised.value.reason_code == "auth_malformed"
    assert str(raised.value) == "OpenCode authentication unavailable"
    assert "secret" not in str(raised.value)
    assert str(auth) not in str(raised.value)


@pytest.mark.parametrize("provider", ["openai", "anthropic", "proxy"])
def test_api_provider_uses_ambient_key_and_stored_entry_wins(tmp_path: Path, provider: str) -> None:
    name = provider.upper() + "_API_KEY"
    env = _env(tmp_path, MIMIR_MODEL_SPEC=provider + ":model", **{name: "ambient-value"})
    invocation = resolve_opencode_invocation(env=env)
    assert invocation.auth_type == "api"
    assert invocation.auth_entry == {"type": "api", "key": "ambient-value"}
    assert json.loads(opencode_worker_documents(invocation, env).auth_document or b"{}")[provider]["key"] == "ambient-value"
    assert "ambient-value" not in repr(invocation)
    _write_auth(tmp_path, {provider: {"type": "api", "key": "stored-value"}})
    assert resolve_opencode_invocation(env=env).auth_entry == {"type": "api", "key": "stored-value"}


def test_codex_newer_reseeds_and_preserves_other_entries(tmp_path: Path) -> None:
    _codex(tmp_path, 500)
    _write_auth(tmp_path, {"openai": {"type": "oauth", "access": _jwt(200), "refresh": "old"},
                           "anthropic": {"type": "api", "key": "unchanged"}})
    resolved = resolve_opencode_invocation(env=_env(tmp_path, MIMIR_MODEL_SPEC="codex-plus:model", OPENAI_API_KEY="metered"))
    entries = json.loads(resolved.auth_path.read_text())
    assert entries["openai"] == {"type": "oauth", "access": _jwt(500), "refresh": "refresh-500", "expires": 500000}
    assert entries["anthropic"] == {"type": "api", "key": "unchanged"}
    assert resolved.auth_path.stat().st_mode & 0o777 == 0o600
    assert resolved.remove_env == ("OPENAI_API_KEY",)
    assert "plugin" not in json.loads(opencode_worker_documents(resolved).config_document)


def test_opencode_newer_writes_back_codex_fields_and_same_refresh_is_noop(tmp_path: Path) -> None:
    original = _codex(tmp_path, 200)
    _write_auth(tmp_path, {"openai": {"type": "oauth", "access": _jwt(500), "refresh": "new"}})
    env = _env(tmp_path, MIMIR_MODEL_SPEC="codex-plus:model")
    resolved = resolve_opencode_invocation(env=env)
    document = json.loads((tmp_path / ".codex" / "auth.json").read_text())
    assert document["tokens"] == {**original["tokens"], "access_token": _jwt(500), "refresh_token": "new"}
    assert document["operator_field"] == "preserved" and document["last_refresh"]
    assert (tmp_path / ".codex" / "auth.json").stat().st_mode & 0o777 == 0o600
    before = ((tmp_path / ".codex" / "auth.json").stat().st_mtime_ns, resolved.auth_path.stat().st_mtime_ns)
    reconcile_codex_auth(env)
    assert before == ((tmp_path / ".codex" / "auth.json").stat().st_mtime_ns, resolved.auth_path.stat().st_mtime_ns)


def test_worker_plugin_refresh_is_reconciled_back_to_both_stores(tmp_path: Path) -> None:
    _codex(tmp_path, 200)
    env = _env(tmp_path, MIMIR_MODEL_SPEC="codex-plus:model")
    resolved = resolve_opencode_invocation(env=env)
    refreshed = tmp_path / "runtime" / "auth.json"
    refreshed.parent.mkdir()
    refreshed.write_text(json.dumps({"openai": {"type": "oauth", "access": _jwt(500), "refresh": "worker-new"}}))
    reconcile_codex_auth(env, refreshed_path=refreshed)
    assert json.loads((tmp_path / ".codex/auth.json").read_text())["tokens"]["refresh_token"] == "worker-new"
    assert json.loads(resolved.auth_path.read_text())["openai"]["refresh"] == "worker-new"


def test_stale_worker_refresh_does_not_overwrite_newer_codex(tmp_path: Path) -> None:
    _codex(tmp_path, 500)
    env = _env(tmp_path, MIMIR_MODEL_SPEC="codex-plus:model")
    reconcile_codex_auth(env)
    refreshed = tmp_path / "runtime/auth.json"
    refreshed.parent.mkdir()
    refreshed.write_text(json.dumps({"openai": {"type": "oauth", "access": _jwt(200), "refresh": "spent"}}))
    path = tmp_path / ".local/share/opencode/auth.json"
    before = path.stat().st_mtime_ns
    reconcile_codex_auth(env, refreshed_path=refreshed)
    assert json.loads((tmp_path / ".codex/auth.json").read_text())["tokens"]["refresh_token"] == "refresh-500"
    assert json.loads(path.read_text())["openai"]["refresh"] == "refresh-500"
    assert path.stat().st_mtime_ns == before


def test_worklink_refreshes_projected_auth_immediately_before_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mimir_cultivate.worklink.backends import opencode as backend_module
    from mimir_cultivate.worklink.backends.base import WorkOrder

    monkeypatch.setattr(backend_module, "_coding_enabled", lambda: True)
    _codex(tmp_path, 200)
    env = _env(tmp_path, MIMIR_MODEL_SPEC="codex-plus:model")
    backend = backend_module.OpenCodeBackend()
    env["XDG_CONFIG_HOME"] = str(tmp_path / ".config")
    env["XDG_DATA_HOME"] = str(tmp_path / ".local/share")
    spec = backend.work_spec(
        WorkOrder(2020, tmp_path, "prompt", None, 30, env),
        attempt=1, repo_url="u", base_ref="main", branch="issue/2020-a1", test_command="true",
    )
    codex_path = tmp_path / ".codex/auth.json"
    document = json.loads(codex_path.read_text())
    document["tokens"].update(access_token=_jwt(500), refresh_token="new-family-token")
    codex_path.write_text(json.dumps(document))
    refreshed = backend.refresh_codex_projections(spec, env)
    assert json.loads(spec.backend_config["worker_projections"][1].document)["openai"]["refresh"] == "refresh-200"
    assert json.loads(refreshed.backend_config["worker_projections"][1].document)["openai"]["refresh"] == "new-family-token"


@pytest.mark.parametrize("plugins", [None, [], ["file:///opt/opencode-feature-factory/plugin/index.js"],
                                     ["opencode-openai-codex-auth@4.4.0"]])
def test_codex_native_oauth_needs_no_plugin_and_projects_none(tmp_path: Path, plugins: list[str] | None) -> None:
    _codex(tmp_path)
    if plugins is not None:
        config = tmp_path / ".config/opencode/opencode.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"plugin": plugins}))
    env = _env(tmp_path, MIMIR_MODEL_SPEC="codex-plus:model", OPENAI_API_KEY="metered-key")
    invocation = resolve_opencode_invocation(env=env)
    documents = opencode_worker_documents(invocation, env)
    assert invocation.auth_type == "oauth"
    assert invocation.remove_env == ("OPENAI_API_KEY",)
    assert "OPENAI_API_KEY" not in invocation.pass_env
    assert "plugin" not in json.loads(documents.config_document)
    assert json.loads(documents.auth_document)["openai"]["type"] == "oauth"
    assert "metered-key" not in repr(invocation)


@pytest.mark.parametrize("coding_enabled", [False, True])
def test_codex_worklink_never_passes_ambient_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, coding_enabled: bool) -> None:
    from mimir_cultivate.worklink.backends import opencode as backend_module
    from mimir_cultivate.worklink.backends.base import WorkOrder

    monkeypatch.setattr(backend_module, "_coding_enabled", lambda: coding_enabled)
    monkeypatch.setenv("OPENAI_API_KEY", "metered-key")
    _codex(tmp_path)
    env = _env(tmp_path, MIMIR_MODEL_SPEC="codex-plus:model", OPENAI_API_KEY="metered-key",
               XDG_CONFIG_HOME=str(tmp_path / ".config"), XDG_DATA_HOME=str(tmp_path / ".local/share"))
    spec = backend_module.OpenCodeBackend().work_spec(
        WorkOrder(2020, tmp_path, "prompt", None, 30, env),
        attempt=1, repo_url="u", base_ref="main", branch="issue/2020-a1", test_command="true",
    )
    # Uncontained runs must explicitly override the inherited parent key;
    # contained runs carry only the credential-free allowlist and OAuth projection.
    if coding_enabled:
        assert "OPENAI_API_KEY" not in spec.env
        assert "OPENAI_API_KEY" not in spec.backend_config["pass_env"]
        projections = spec.backend_config["worker_projections"]
        assert "plugin" not in json.loads(projections[0].document)
        assert json.loads(projections[1].document)["openai"]["type"] == "oauth"
    else:
        assert spec.env["OPENAI_API_KEY"] == ""
    assert "metered-key" not in str(spec.local_argv)


def test_codex_invalid_bundle_fails_without_secrets(tmp_path: Path) -> None:
    _codex(tmp_path)
    env = _env(tmp_path, MIMIR_MODEL_SPEC="codex-plus:model", OPENAI_API_KEY="metered-key")
    (tmp_path / ".codex" / "auth.json").write_text(json.dumps({"auth_mode": "apikey"}))
    with pytest.raises(OpenCodeAuthError) as raised:
        resolve_opencode_invocation(env=env)
    assert raised.value.reason_code == "auth_missing"
    assert "metered-key" not in repr(raised.value)
    document = _codex(tmp_path)
    document["tokens"]["access_token"] = "not-a-jwt"
    (tmp_path / ".codex/auth.json").write_text(json.dumps(document))
    with pytest.raises(OpenCodeAuthError) as raised:
        resolve_opencode_invocation(env=env)
    assert raised.value.reason_code == "auth_invalid"


@pytest.mark.asyncio
async def test_worklink_reconciles_before_and_after_invocation(tmp_path: Path) -> None:
    from mimir_cultivate.worklink.backends.opencode import OpenCodeBackend
    from mimir_cultivate.worklink.compute import ComputeResult

    _codex(tmp_path, 200)
    env = _env(tmp_path, MIMIR_MODEL_SPEC="codex-plus:model")
    path = tmp_path / ".local/share/opencode/auth.json"
    worker = tmp_path / "runtime/auth.json"
    worker.parent.mkdir()

    async def invoke() -> ComputeResult:
        assert json.loads(path.read_text())["openai"]["refresh"] == "refresh-200"
        worker.write_text(json.dumps({"openai": {"type": "oauth", "access": _jwt(500), "refresh": "refreshed"}}))
        return ComputeResult(exit_code=0, stdout="", stderr="")

    result = await OpenCodeBackend().invoke_with_startup_retry(
        invoke, issue_id=1, checkout_snapshot=lambda: (), codex=True, env=env,
        refreshed_path=worker,
    )
    assert result.exit_code == 0
    assert json.loads((tmp_path / ".codex/auth.json").read_text())["tokens"]["refresh_token"] == "refreshed"


def test_oversize_config_is_refused_before_parsing(tmp_path: Path) -> None:
    config = tmp_path / "opencode.json"
    config.write_bytes(b"{" + b" " * (1024 * 1024))

    with pytest.raises(OpenCodeConfigError) as raised:
        resolve_opencode_invocation(
            env=_env(tmp_path, OPENCODE_CONFIG=str(config))
        )

    assert raised.value.reason_code == "config_oversize"
    assert str(raised.value) == "OpenCode configuration unavailable"


def test_worker_documents_materialize_env_without_repr_or_scrub_leaks(
    tmp_path: Path,
) -> None:
    config = tmp_path / "operator" / "opencode.json"
    config.parent.mkdir()
    config.write_text(
        '{"model":"proxy/model","provider":{"proxy":{"options":{"apiKey":"{env:PROXY_TOKEN}"}}}}',
        encoding="utf-8",
    )
    _write_auth(tmp_path, {
        "proxy": {"type": "oauth", "refresh": "oauth-refresh-secret"},
        "inactive": {"type": "api", "key": "inactive-auth-secret"},
    })
    env = _env(
        tmp_path,
        OPENCODE_CONFIG=str(config),
        PROXY_TOKEN="projected-env-secret",
    )

    invocation = resolve_opencode_invocation(env=env)
    documents = opencode_worker_documents(invocation, env)

    assert json.loads(documents.config_document)["provider"]["proxy"]["options"]["apiKey"] == "projected-env-secret"
    assert "projected-env-secret" not in repr(documents)
    assert str(config) not in repr(invocation)
    text = documents.scrubber.scrub_text(
        f"{config} projected-env-secret oauth-refresh-secret inactive-auth-secret"
    )
    assert str(config) not in text
    assert "projected-env-secret" not in text
    assert "oauth-refresh-secret" not in text
    assert "inactive-auth-secret" not in text
