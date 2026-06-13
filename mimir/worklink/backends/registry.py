"""Config-driven Worklink backend selection."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from ..compute import ComputeBackend, LocalSubprocessComputeBackend
from .base import ToolBackend
from .claude_cli import ClaudeCliBackend
from .codex import CodexBackend


@dataclass(frozen=True)
class WorklinkDefaults:
    backend: str = "codex"
    timeout_s: int = 1800
    priority: str = "normal"
    test_command: str = "env -u MIMIR_MODEL_SPEC uv run pytest -q"
    backend_by_category: Mapping[str, str] = field(default_factory=dict)
    compute_backend: str = "local_subprocess"


@dataclass(frozen=True)
class ToolPin:
    name: str
    category: str
    pin: str
    smoke: str
    source: str | None = None
    package: str | None = None
    repo: str | None = None
    install: str | None = None
    risk: str | None = None


@dataclass(frozen=True)
class WorklinkRoute:
    backend: str
    label: str | None = None
    repo: str | None = None
    tool_category: str | None = None
    compute_backend: str | None = None

    def matches(self, *, labels: set[str], repo: str | None, tool_category: str | None) -> bool:
        if self.label is not None and self.label not in labels:
            return False
        if self.repo is not None and self.repo != repo:
            return False
        if self.tool_category is not None and self.tool_category != tool_category:
            return False
        return self.label is not None or self.repo is not None or self.tool_category is not None


@dataclass(frozen=True)
class WorklinkConfig:
    defaults: WorklinkDefaults = field(default_factory=WorklinkDefaults)
    routes: tuple[WorklinkRoute, ...] = ()
    backend_settings: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    tool_pins: tuple[ToolPin, ...] = ()

    @classmethod
    def load(cls, path: Path) -> "WorklinkConfig":
        if not path.exists():
            return cls()
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError("worklink config root must be a mapping")
        defaults_data = data.get("defaults") or {}
        if not isinstance(defaults_data, dict):
            raise ValueError("worklink defaults must be a mapping")
        category_defaults = defaults_data.get("backend_by_category") or defaults_data.get("category_defaults") or {}
        if not isinstance(category_defaults, dict):
            raise ValueError("worklink category defaults must be a mapping")
        defaults = WorklinkDefaults(
            backend=str(defaults_data.get("backend", "codex")),
            timeout_s=int(defaults_data.get("timeout_s", 1800)),
            priority=str(defaults_data.get("priority", "normal")),
            test_command=str(defaults_data.get("test_command", "env -u MIMIR_MODEL_SPEC uv run pytest -q")),
            backend_by_category={str(key): str(value) for key, value in category_defaults.items()},
            compute_backend=str(defaults_data.get("compute_backend", "local_subprocess")),
        )
        routes = tuple(_parse_route(route) for route in data.get("routes") or ())
        tool_pins = _parse_tool_pins(data.get("tool_pins") or [])
        backends = data.get("backends") or {}
        if not isinstance(backends, dict):
            raise ValueError("worklink backends must be a mapping")
        return cls(defaults=defaults, routes=routes, backend_settings=backends, tool_pins=tool_pins)

    def select_compute_backend_name(
        self,
        *,
        labels: set[str] | None = None,
        repo: str | None = None,
        tool_category: str | None = None,
    ) -> str:
        label_set = labels or set()
        for route in self.routes:
            if route.matches(labels=label_set, repo=repo, tool_category=tool_category):
                return route.compute_backend or self.defaults.compute_backend
        return self.defaults.compute_backend

    def select_backend_name(
        self,
        *,
        labels: set[str] | None = None,
        repo: str | None = None,
        tool_category: str | None = None,
    ) -> str:
        label_set = labels or set()
        for route in self.routes:
            if route.matches(labels=label_set, repo=repo, tool_category=tool_category):
                return route.backend
        if tool_category and tool_category in self.defaults.backend_by_category:
            return self.defaults.backend_by_category[tool_category]
        return self.defaults.backend


def _parse_tool_pins(value: Any) -> tuple[ToolPin, ...]:
    if not isinstance(value, list):
        raise ValueError("worklink tool_pins must be a list")
    return tuple(_parse_tool_pin(item, index=index) for index, item in enumerate(value))


def _parse_tool_pin(value: Any, *, index: int) -> ToolPin:
    if not isinstance(value, dict):
        raise ValueError(f"worklink tool_pins[{index}] must be a mapping")
    missing = [field for field in ("name", "category", "pin", "smoke") if field not in value]
    if missing:
        raise ValueError(f"worklink tool_pins[{index}] missing required field(s): {', '.join(missing)}")
    return ToolPin(
        name=str(value["name"]),
        category=str(value["category"]),
        pin=str(value["pin"]),
        smoke=str(value["smoke"]),
        source=str(value["source"]) if "source" in value else None,
        package=str(value["package"]) if "package" in value else None,
        repo=str(value["repo"]) if "repo" in value else None,
        install=str(value["install"]) if "install" in value else None,
        risk=str(value["risk"]) if "risk" in value else None,
    )


def _parse_route(value: Any) -> WorklinkRoute:
    if not isinstance(value, dict):
        raise ValueError("worklink route must be a mapping")
    backend = value.get("backend")
    if not backend:
        raise ValueError("worklink route missing backend")
    return WorklinkRoute(
        backend=str(backend),
        label=str(value["label"]) if "label" in value else None,
        repo=str(value["repo"]) if "repo" in value else None,
        tool_category=str(value["tool_category"]) if "tool_category" in value else None,
        compute_backend=str(value["compute_backend"]) if "compute_backend" in value else None,
    )


class BackendRegistry:
    def __init__(self, config: WorklinkConfig | None = None) -> None:
        self.config = config or WorklinkConfig()
        self._backends: dict[str, ToolBackend] = {
            "codex": self._build_codex(self.config.backend_settings.get("codex", {})),
            "claude_cli": self._build_claude_cli(
                self.config.backend_settings.get("claude_cli", {})
            ),
        }
        self._compute_backends: dict[str, ComputeBackend] = {
            "local_subprocess": LocalSubprocessComputeBackend(),
        }

    def register(self, backend: ToolBackend) -> None:
        self._backends[backend.name] = backend

    def register_compute(self, backend: ComputeBackend) -> None:
        self._compute_backends[backend.name] = backend

    def get(self, name: str) -> ToolBackend:
        try:
            return self._backends[name]
        except KeyError as exc:
            raise KeyError(f"unknown Worklink backend: {name}") from exc

    def get_compute(self, name: str) -> ComputeBackend:
        try:
            return self._compute_backends[name]
        except KeyError as exc:
            raise KeyError(f"unknown Worklink compute backend: {name}") from exc

    def select_compute(
        self,
        *,
        labels: set[str] | None = None,
        repo: str | None = None,
        tool_category: str | None = None,
    ) -> ComputeBackend:
        return self.get_compute(
            self.config.select_compute_backend_name(
                labels=labels, repo=repo, tool_category=tool_category
            )
        )

    def select(
        self,
        *,
        labels: set[str] | None = None,
        repo: str | None = None,
        tool_category: str | None = None,
    ) -> ToolBackend:
        return self.get(
            self.config.select_backend_name(labels=labels, repo=repo, tool_category=tool_category)
        )

    @staticmethod
    def _build_codex(settings: Mapping[str, Any]) -> CodexBackend:
        bin_name = str(settings.get("bin", "codex"))
        args = settings.get("args", ["exec", "--json"])
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            raise ValueError("worklink codex args must be a list of strings")
        return CodexBackend(bin=bin_name, extra_args=tuple(args))

    @staticmethod
    def _build_claude_cli(settings: Mapping[str, Any]) -> ClaudeCliBackend:
        bin_name = str(settings.get("bin", "claude"))
        args = settings.get("args", ["-p", "--output-format", "json"])
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            raise ValueError("worklink claude_cli args must be a list of strings")
        return ClaudeCliBackend(bin=bin_name, extra_args=tuple(args))
