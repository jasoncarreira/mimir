from __future__ import annotations

import json
from pathlib import Path

import pytest

from mimir.opencode_config import (
    OpenCodeAuthError,
    OpenCodeConfigError,
    opencode_config_path,
    opencode_worker_documents,
    resolve_opencode_invocation,
)


def _env(tmp_path: Path, **values: str) -> dict[str, str]:
    return {"HOME": str(tmp_path), **values}


def _write_auth(tmp_path: Path, payload: object) -> None:
    path = tmp_path / ".local" / "share" / "opencode" / "auth.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


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
        ("codex-plus:gpt-5.6-luna", "openai/gpt-5.6-luna"),
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
    _write_auth(tmp_path, {"openai": {"type": "oauth", "refresh": "oauth-secret"}})
    resolved = resolve_opencode_invocation(
        env=_env(
            tmp_path,
            MIMIR_MODEL_SPEC="codex-plus:gpt-5.6-luna",
            OPENAI_API_KEY="metered-secret",
        )
    )
    assert resolved.auth_type == "oauth"
    assert resolved.remove_env == ("OPENAI_API_KEY",)
    assert "oauth-secret" not in repr(resolved)
    assert "metered-secret" not in repr(resolved)


def test_openai_ambient_key_without_stored_auth_is_rejected(tmp_path: Path) -> None:
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
    assert auth_document is None


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
