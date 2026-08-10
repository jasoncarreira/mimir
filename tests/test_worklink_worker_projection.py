from __future__ import annotations

import json
from pathlib import Path

from mimir.worklink.backends.base import WorkOrder
from mimir.worklink.backends.opencode import OpenCodeBackend


def _write_native_files(home: Path) -> None:
    config = home / ".config" / "opencode" / "opencode.jsonc"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps({
            "model": "proxy/model",
            "provider": {
                "proxy": {"options": {"apiKey": "{env:PROXY_TOKEN}"}},
                "inactive": {"options": {"url": "https://inactive.invalid"}},
            },
        }),
        encoding="utf-8",
    )
    auth = home / ".local" / "share" / "opencode" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(json.dumps({
        "proxy": {"type": "oauth", "access": "access", "refresh": "refresh", "expires": 7},
        "inactive": {"type": "api", "key": "inactive"},
    }), encoding="utf-8")


def test_enabled_opencode_spec_contains_worker_local_selected_projections(
    monkeypatch, tmp_path: Path
) -> None:
    _write_native_files(tmp_path)
    monkeypatch.setenv("MIMIR_CODING_ENABLED", "true")
    monkeypatch.setenv("HOME", str(tmp_path))
    # Redirecting HOME alone does not seal config resolution: opencode_config
    # prefers XDG_CONFIG_HOME and only falls back to HOME/.config. Left set,
    # resolution escapes tmp_path into the real config directory and picks up
    # whatever providers it declares -- so this test passes where the variable
    # is unset and fails where it is set, which is ambient state it does not own.
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("PROXY_TOKEN", "referenced")
    order = WorkOrder(
        issue_id=1410,
        checkout=tmp_path / "checkout",
        prompt="build",
        rules=None,
        timeout_s=30,
        env={"MIMIR_HOME": str(tmp_path), "UNRELATED": "omitted"},
    )

    spec = OpenCodeBackend().work_spec(
        order,
        attempt=1,
        repo_url="repo",
        base_ref="main",
        branch="issue/1410-a1",
        test_command="true",
    )

    projections = spec.backend_config["worker_projections"]
    assert [projection.path for projection in projections] == [
        ".config/opencode/opencode.json",
        ".local/share/opencode/auth.json",
    ]
    assert json.loads(projections[0].document) == {
        "model": "proxy/model",
        "provider": {"proxy": {"options": {"apiKey": "{env:PROXY_TOKEN}"}}},
    }
    assert json.loads(projections[1].document) == {
        "proxy": {"type": "oauth", "access": "access", "refresh": "refresh", "expires": 7}
    }
    assert spec.backend_config["pass_env"] == ("PROXY_TOKEN",)
    assert spec.env == {
        "PROXY_TOKEN": "referenced",
        "OPENCODE_PERMISSION": spec.env["OPENCODE_PERMISSION"],
    }


def test_disabled_opencode_spec_preserves_direct_environment(
    monkeypatch, tmp_path: Path
) -> None:
    _write_native_files(tmp_path)
    monkeypatch.delenv("MIMIR_CODING_ENABLED", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    # See the note above: HOME alone does not seal opencode config resolution.
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    order = WorkOrder(
        issue_id=1410,
        checkout=tmp_path / "checkout",
        prompt="build",
        rules=None,
        timeout_s=30,
        env={"MIMIR_HOME": str(tmp_path), "PROXY_TOKEN": "direct"},
    )

    spec = OpenCodeBackend().work_spec(
        order,
        attempt=1,
        repo_url="repo",
        base_ref="main",
        branch="issue/1410-a1",
        test_command="true",
    )

    assert "worker_projections" not in spec.backend_config
    assert spec.env["PROXY_TOKEN"] == "direct"
    assert spec.env["MIMIR_HOME"] == str(tmp_path)
