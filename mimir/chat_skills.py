"""Allowlisted chat-skill discovery and slash-command parsing."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .config import Config, _normalize_chat_skill_name
from .skill_catalog import load_skill
from .skill_defs import home_builtin_skills_dir, home_skills_dir
from .skill_md import strip_frontmatter


CHAT_SKILL_EXTRA_KEY = "chat_skill_invocation"


@dataclass(frozen=True)
class ChatSkillDescriptor:
    name: str
    command: str
    label: str
    description: str


@dataclass(frozen=True)
class ChatSkillInvocation:
    name: str
    command: str
    args: str
    raw: str


@dataclass(frozen=True)
class ChatSkillError:
    code: str
    message: str
    command: str
    raw: str


@dataclass(frozen=True)
class _EffectiveChatSkill:
    descriptor: ChatSkillDescriptor
    prompt_body: str


@dataclass(frozen=True)
class ChatSkillRegistry:
    enabled: bool
    allowlist: tuple[str, ...]
    _discoverable: tuple[_EffectiveChatSkill, ...] = ()

    @classmethod
    def from_config(cls, config: Config) -> "ChatSkillRegistry":
        effective = _load_effective_chat_skills(config.home)
        discoverable = tuple(
            effective[name]
            for name in config.chat_skill_allowlist
            if name in effective
        )
        return cls(
            enabled=config.chat_skills_enabled,
            allowlist=config.chat_skill_allowlist,
            _discoverable=discoverable,
        )

    def list_discoverable(self) -> tuple[ChatSkillDescriptor, ...]:
        if not self.enabled:
            return ()
        return tuple(skill.descriptor for skill in self._discoverable)

    def resolve_post_content(
        self,
        content: str,
    ) -> ChatSkillInvocation | ChatSkillError | None:
        if not self.enabled:
            return None
        trimmed = (content or "").lstrip()
        if not trimmed.startswith("/"):
            return None
        parts = trimmed.split(None, 1)
        command_token = parts[0]
        arg_tail = parts[1] if len(parts) > 1 else ""
        if command_token == "/":
            return ChatSkillError(
                code="invalid_command",
                message="Chat skill command must include a skill name after '/'.",
                command="/",
                raw=content,
            )
        raw_name = command_token[1:]
        if not raw_name or raw_name.startswith("/"):
            return ChatSkillError(
                code="invalid_command",
                message="Malformed chat skill command.",
                command=command_token,
                raw=content,
            )
        name = _normalize_chat_skill_name(raw_name)
        if name is None:
            return ChatSkillError(
                code="invalid_skill_name",
                message="Chat skill name must be lowercase letters, digits, and hyphens.",
                command=command_token,
                raw=content,
            )
        skill = self._find_skill(name)
        if skill is None:
            return ChatSkillError(
                code="skill_unavailable",
                message=f"Chat skill '/{name}' is not available.",
                command=f"/{name}",
                raw=content,
            )
        return ChatSkillInvocation(
            name=name,
            command=f"/{name}",
            args=arg_tail.strip(),
            raw=content,
        )

    def prompt_block_for_invocation(self, invocation_dict: Mapping[str, Any] | None) -> str | None:
        if not self.enabled or not isinstance(invocation_dict, Mapping):
            return None
        invocation = _invocation_from_dict(invocation_dict)
        if invocation is None:
            return None
        parsed = self.resolve_post_content(invocation.raw)
        if not isinstance(parsed, ChatSkillInvocation) or parsed != invocation:
            return None
        skill = self._find_skill(invocation.name)
        if skill is None:
            return None
        lines = [
            f"## Chat skill command: {skill.descriptor.command}",
            "",
            f"The user invoked the allowlisted chat skill `{skill.descriptor.command}`.",
        ]
        if skill.descriptor.label:
            lines.append(f"Skill: {skill.descriptor.label}")
        if skill.descriptor.description:
            lines.append(f"Description: {skill.descriptor.description}")
        lines.extend(
            [
                f"Arguments: {invocation.args or '(none)'}",
                "",
                "Follow these skill instructions for this request:",
            ]
        )
        if skill.prompt_body:
            lines.extend(["", skill.prompt_body])
        return "\n".join(lines).strip()

    def _find_skill(self, name: str) -> _EffectiveChatSkill | None:
        for skill in self._discoverable:
            if skill.descriptor.name == name:
                return skill
        return None


def _invocation_from_dict(invocation_dict: Mapping[str, Any]) -> ChatSkillInvocation | None:
    name = invocation_dict.get("name")
    command = invocation_dict.get("command")
    args = invocation_dict.get("args")
    raw = invocation_dict.get("raw")
    if not all(isinstance(value, str) for value in (name, command, args, raw)):
        return None
    normalized_name = _normalize_chat_skill_name(name)
    if normalized_name is None or command != f"/{normalized_name}":
        return None
    return ChatSkillInvocation(
        name=normalized_name,
        command=command,
        args=args,
        raw=raw,
    )


def _load_effective_chat_skills(home: Path) -> dict[str, _EffectiveChatSkill]:
    out: dict[str, _EffectiveChatSkill] = {}
    for skills_root in (home_builtin_skills_dir(home), home_skills_dir(home)):
        if not skills_root.is_dir():
            continue
        try:
            entries = sorted(skills_root.iterdir())
        except OSError:
            continue
        for skill_dir in entries:
            skill = _load_effective_chat_skill(skill_dir)
            if skill is None:
                continue
            out[skill.descriptor.name] = skill
    return out


def _load_effective_chat_skill(skill_dir: Path) -> _EffectiveChatSkill | None:
    if not skill_dir.is_dir():
        return None
    name = _normalize_chat_skill_name(skill_dir.name)
    if name is None:
        return None
    entry = load_skill(skill_dir)
    if entry is None:
        return None
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return None
    try:
        body = strip_frontmatter(skill_md.read_text(encoding="utf-8")).strip()
    except OSError:
        return None
    label = entry.name.strip() or name
    description = entry.description.strip()
    return _EffectiveChatSkill(
        descriptor=ChatSkillDescriptor(
            name=name,
            command=f"/{name}",
            label=label,
            description=description,
        ),
        prompt_body=body,
    )
