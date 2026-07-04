"""Tests for allowlisted chat skill discovery and slash-command parsing."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pytest

from mimir.chat_skills import (
    CHAT_SKILL_EXTRA_KEY,
    ChatSkillError,
    ChatSkillInvocation,
    ChatSkillRegistry,
)
from mimir.config import Config
from mimir.skill_defs import home_builtin_skills_dir, home_skills_dir


def _config(
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
    *,
    enabled: bool,
    allowlist: str = "",
) -> Config:
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv("MIMIR_CLAUDE_OAUTH_CREDENTIALS", "")
    monkeypatch.setenv("MIMIR_CHAT_SKILLS_ENABLED", "true" if enabled else "false")
    if allowlist:
        monkeypatch.setenv("MIMIR_CHAT_SKILL_ALLOWLIST", allowlist)
    else:
        monkeypatch.delenv("MIMIR_CHAT_SKILL_ALLOWLIST", raising=False)
    return Config.from_env()


def _make_skill(
    root: Path,
    slug: str,
    *,
    label: str | None = None,
    description: str,
    body: str,
) -> Path:
    skill_dir = root / slug
    skill_dir.mkdir(parents=True)
    lines = ["---"]
    if label is not None:
        lines.append(f"name: {label}")
    lines.append(f"description: {description}")
    lines.extend(["---", body])
    (skill_dir / "SKILL.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return skill_dir


def test_registry_disabled_is_inert(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _make_skill(
        home_builtin_skills_dir(tmp_path),
        "memory",
        label="Memory",
        description="Capture durable context.",
        body="Use memory carefully.",
    )
    cfg = _config(monkeypatch, tmp_path, enabled=False, allowlist="memory")

    registry = ChatSkillRegistry.from_config(cfg)

    assert registry.enabled is False
    assert registry.list_discoverable() == ()
    assert registry.resolve_post_content("/memory remember this") is None


def test_enabled_empty_allowlist_disables_discovery_but_rejects_slash_commands(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _make_skill(
        home_builtin_skills_dir(tmp_path),
        "memory",
        description="Capture durable context.",
        body="Use memory carefully.",
    )
    cfg = _config(monkeypatch, tmp_path, enabled=True)

    registry = ChatSkillRegistry.from_config(cfg)
    resolved = registry.resolve_post_content("/memory remember this")

    assert registry.enabled is True
    assert registry.list_discoverable() == ()
    assert isinstance(resolved, ChatSkillError)
    assert resolved.code == "skill_unavailable"
    assert resolved.command == "/memory"


def test_registry_uses_allowlist_order_and_operator_skill_shadowing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _make_skill(
        home_builtin_skills_dir(tmp_path),
        "memory",
        label="Memory",
        description="Bundled description.",
        body="Bundled memory body.",
    )
    _make_skill(
        home_skills_dir(tmp_path),
        "memory",
        label="Memory+",
        description="Operator description.",
        body="Operator memory body.",
    )
    _make_skill(
        home_builtin_skills_dir(tmp_path),
        "github",
        label="GitHub",
        description="Work with pull requests.",
        body="Use GitHub workflows.",
    )
    cfg = _config(monkeypatch, tmp_path, enabled=True, allowlist="/github, memory, missing")

    registry = ChatSkillRegistry.from_config(cfg)
    discoverable = registry.list_discoverable()
    invocation = ChatSkillInvocation(
        name="memory",
        command="/memory",
        args="refresh state",
        raw="/memory refresh state",
    )
    block = registry.prompt_block_for_invocation(asdict(invocation))

    assert CHAT_SKILL_EXTRA_KEY == "chat_skill_invocation"
    assert [skill.name for skill in discoverable] == ["github", "memory"]
    assert [skill.command for skill in discoverable] == ["/github", "/memory"]
    assert discoverable[0].label == "GitHub"
    assert discoverable[1].label == "Memory+"
    assert discoverable[1].description == "Operator description."
    assert set(asdict(discoverable[0])) == {"name", "command", "label", "description"}
    assert block is not None
    assert "Operator memory body." in block
    assert "Bundled memory body." not in block
    assert "---" not in block
    assert str(tmp_path) not in block


def test_resolve_post_content_parses_leading_slash_commands(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _make_skill(
        home_builtin_skills_dir(tmp_path),
        "memory",
        description="Capture durable context.",
        body="Use memory carefully.",
    )
    cfg = _config(monkeypatch, tmp_path, enabled=True, allowlist="memory")

    registry = ChatSkillRegistry.from_config(cfg)
    resolved = registry.resolve_post_content("   /memory   capture this context   ")

    assert isinstance(resolved, ChatSkillInvocation)
    assert resolved.name == "memory"
    assert resolved.command == "/memory"
    assert resolved.args == "capture this context"
    assert resolved.raw == "   /memory   capture this context   "
    assert registry.resolve_post_content("please use /memory here") is None


def test_resolve_post_content_returns_structured_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _make_skill(
        home_builtin_skills_dir(tmp_path),
        "memory",
        description="Capture durable context.",
        body="Use memory carefully.",
    )
    cfg = _config(monkeypatch, tmp_path, enabled=True, allowlist="memory")

    registry = ChatSkillRegistry.from_config(cfg)
    malformed = registry.resolve_post_content("/")
    invalid = registry.resolve_post_content("/memory!")
    unavailable = registry.resolve_post_content("/github")

    assert isinstance(malformed, ChatSkillError)
    assert malformed.code == "invalid_command"
    assert isinstance(invalid, ChatSkillError)
    assert invalid.code == "invalid_skill_name"
    assert isinstance(unavailable, ChatSkillError)
    assert unavailable.code == "skill_unavailable"


def test_prompt_block_rejects_tampered_invocation_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _make_skill(
        home_builtin_skills_dir(tmp_path),
        "memory",
        description="Capture durable context.",
        body="Use memory carefully.",
    )
    cfg = _config(monkeypatch, tmp_path, enabled=True, allowlist="memory")

    registry = ChatSkillRegistry.from_config(cfg)

    assert registry.prompt_block_for_invocation(
        {
            "name": "memory",
            "command": "/github",
            "args": "remember this",
            "raw": "/memory remember this",
        }
    ) is None
