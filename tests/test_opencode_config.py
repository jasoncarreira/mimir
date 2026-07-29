from __future__ import annotations

import json
from pathlib import Path

import pytest

from mimir.opencode_config import (
    OpenCodeAuthError,
    OpenCodeConfigError,
    opencode_config_path,
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
    with pytest.raises(OpenCodeAuthError, match="provider 'openai'.*metered fallback"):
        resolve_opencode_invocation(
            env=_env(
                tmp_path,
                MIMIR_MODEL_SPEC="codex-plus:gpt-5.6-luna",
                OPENAI_API_KEY="must-not-appear",
            )
        )


def test_missing_provider_credential_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(OpenCodeConfigError, match="provider 'anthropic'.*no credential"):
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

    with pytest.raises(OpenCodeAuthError, match="provider 'anthropic'.*no credential"):
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
