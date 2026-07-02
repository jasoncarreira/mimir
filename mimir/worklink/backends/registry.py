"""Config-driven Worklink backend selection."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from ..compute import (
    ComputeBackend,
    ComputeCaps,
    DockerSiblingComputeBackend,
    EcsRunTaskComputeBackend,
    EcsRunTaskConfig,
    LocalSubprocessComputeBackend,
)
from .base import ToolBackend
from .claude_cli import ClaudeCliBackend
from .codex import CodexBackend


WORKLINK_MERGED_LABEL = "worklink:merged"

DEFAULT_HIGH_RISK_SCOPE_PATTERNS: tuple[str, ...] = (
    "**/migrations/**",
    "**/*migration*",
    "**/schema.sql",
    "**/*auth*",
    "**/*oauth*",
    "**/*credential*",
    "**/*secret*",
    "**/generated/**",
    "**/*_pb2.py",
    "*.lock",
    "**/*.lock",
    ".github/workflows/**",
    "**/Dockerfile*",
    "**/*.tf",
)

DEFAULT_HIGH_RISK_LABELS: tuple[str, ...] = (
    "risk:high",
    "security",
    "auth",
    "migration",
    "prod-data",
    "generated-code",
    "hotspot",
)


@dataclass(frozen=True)
class TieredReviewConfig:
    # Default high-risk markers are ecosystem-agnostic glob patterns. A
    # deployment's own sensitive surfaces, such as Worklink internals,
    # access-control code, config, or action guards, belong in worklink.yaml.
    high_risk_scope_patterns: tuple[str, ...] = DEFAULT_HIGH_RISK_SCOPE_PATTERNS
    high_risk_labels: tuple[str, ...] = DEFAULT_HIGH_RISK_LABELS
    # High-risk slices get multi-vote review using this reviewer count; all
    # other slices get one adversarial pass. Do not add a second trigger list
    # unless a real third tier appears.
    multi_vote_reviewer_count: int = 3


@dataclass(frozen=True)
class WorklinkDefaults:
    backend: str = "codex"
    timeout_s: int = 1800
    priority: str = "normal"
    test_command: str = "env -u MIMIR_MODEL_SPEC uv run pytest -q"
    backend_by_category: Mapping[str, str] = field(default_factory=dict)
    compute_backend: str = "local_subprocess"
    # Branch that attempt worktrees are cut from and that leaf PRs target. Point
    # it at a long-running integration/feature branch to stack Worklink leaves
    # there instead of opening every PR straight against main.
    base_branch: str = "main"
    # Refresh origin/<base_branch> before cutting local attempts. The fetch is
    # ref-only and does not update the source checkout's working tree or local
    # branch; this can be disabled for local-only branch testing.
    base_fetch: bool = True
    # Slice-3 autonomy. ``max_concurrent`` caps how many leaves may be
    # claimed (``worklink:in-progress``) at once across autonomous dispatch
    # (poller + tool); the operator CLI is not capped. ``reaper_ttl_s`` is
    # how long a claim may sit without a heartbeat before the TTL reaper
    # steals it back to ready/blocked — kept above 2x ``timeout_s`` so a normal
    # worker timeout plus finalize-time remote test cannot be reaped.
    max_concurrent: int = 2
    reaper_ttl_s: int = 7200
    # Autonomy safety posture (#460). local_subprocess runs the backend with
    # full container-filesystem access (no sandbox) — fine for an operator who
    # accepts the blast radius, unsafe as an autonomous default. Autonomous
    # dispatch (poller / worklink_run tool) REFUSES local_subprocess unless this
    # is flipped true; it always prefers an isolated ComputeBackend
    # (docker_sibling / ecs_runtask). The operator CLI is never gated by this.
    allow_autonomous_local_subprocess: bool = False
    epic_branch_prefix: str = "epic/"
    max_review_retries: int = 3
    reviewer_backend: str | None = None
    tiered_review: TieredReviewConfig = field(default_factory=TieredReviewConfig)

    def __post_init__(self) -> None:
        if self.reviewer_backend is None:
            object.__setattr__(self, "reviewer_backend", self.backend)


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
    compute_backend_settings: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
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
        category_defaults = defaults_data.get("backend_by_category") or defaults_data.get(
            "category_defaults"
        ) or {}
        if not isinstance(category_defaults, dict):
            raise ValueError("worklink category defaults must be a mapping")
        default_values = WorklinkDefaults()
        backend_name = str(defaults_data.get("backend", default_values.backend))
        defaults = WorklinkDefaults(
            backend=backend_name,
            timeout_s=int(defaults_data.get("timeout_s", default_values.timeout_s)),
            priority=str(defaults_data.get("priority", default_values.priority)),
            test_command=str(
                defaults_data.get("test_command", default_values.test_command)
            ),
            backend_by_category={
                str(key): str(value) for key, value in category_defaults.items()
            },
            compute_backend=_normalize_compute_backend_name(
                str(
                    defaults_data.get(
                        "compute_backend",
                        defaults_data.get("compute", default_values.compute_backend),
                    )
                )
            ),
            base_branch=str(defaults_data.get("base_branch", default_values.base_branch)),
            base_fetch=_coerce_safety_bool(defaults_data.get("base_fetch", True), default=True),
            max_concurrent=_positive_int(
                defaults_data.get("max_concurrent"),
                default=WorklinkDefaults.max_concurrent,
            ),
            reaper_ttl_s=_positive_int(
                defaults_data.get("reaper_ttl_s"),
                default=WorklinkDefaults.reaper_ttl_s,
            ),
            allow_autonomous_local_subprocess=_coerce_safety_bool(
                defaults_data.get("allow_autonomous_local_subprocess", False)
            ),
            epic_branch_prefix=str(
                defaults_data.get("epic_branch_prefix", default_values.epic_branch_prefix)
            ),
            max_review_retries=_positive_int(
                defaults_data.get("max_review_retries"),
                default=WorklinkDefaults.max_review_retries,
            ),
            reviewer_backend=str(defaults_data.get("reviewer_backend", backend_name)),
            tiered_review=_parse_tiered_review_config(defaults_data.get("tiered_review")),
        )
        routes = tuple(_parse_route(route) for route in data.get("routes") or ())
        tool_pins = _parse_tool_pins(data.get("tool_pins") or [])
        backends = data.get("backends") or {}
        if not isinstance(backends, dict):
            raise ValueError("worklink backends must be a mapping")
        compute_backends = data.get("compute_backends") or {}
        if not isinstance(compute_backends, dict):
            raise ValueError("worklink compute_backends must be a mapping")
        normalized_compute_backends = {
            _normalize_compute_backend_name(str(name)): _expect_mapping(
                settings, f"worklink compute_backends.{name}"
            )
            for name, settings in compute_backends.items()
        }
        return cls(
            defaults=defaults,
            routes=routes,
            backend_settings=backends,
            compute_backend_settings=normalized_compute_backends,
            tool_pins=tool_pins,
        )

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

    #: Compute substrates that run the backend with full container-filesystem
    #: access (no isolation). Autonomous dispatch refuses these without opt-in.
    UNSANDBOXED_COMPUTE: tuple[str, ...] = ("local_subprocess",)

    def autonomous_compute_allowed(
        self,
        compute_backend_name: str,
        caps: ComputeCaps | None = None,
    ) -> tuple[bool, str | None]:
        """Whether autonomous dispatch may use a compute substrate (#460/#479).

        The safety invariant is capability-based: autonomous dispatch refuses a
        substrate with shared filesystem access or without network isolation
        unless the operator explicitly opts in to local-subprocess blast radius.
        The historical name list remains a secondary guard for aliases of the
        known local backend, but it is not the primary policy surface.
        """
        normalized = _normalize_compute_backend_name(compute_backend_name)
        unsafe_by_name = normalized in self.UNSANDBOXED_COMPUTE
        unsafe_by_caps = caps is not None and (caps.shared_filesystem or not caps.network_isolated)
        if (unsafe_by_name or unsafe_by_caps) and not self.defaults.allow_autonomous_local_subprocess:
            reason = "shared filesystem access" if caps and caps.shared_filesystem else "no network isolation"
            if unsafe_by_name and caps is None:
                reason = "known unsandboxed compute backend"
            return False, (
                f"autonomous Worklink dispatch refuses the unsandboxed "
                f"'{compute_backend_name}' compute backend ({reason}). Route this "
                f"issue to an isolated ComputeBackend (docker_sibling / ecs_runtask), "
                f"or set defaults.allow_autonomous_local_subprocess: true in "
                f"worklink.yaml to accept the blast radius for autonomous runs. "
                f"The operator CLI `mimir worklink run` is unaffected."
            )
        return True, None


def _positive_int(value: Any, *, default: int) -> int:
    """Coerce positive integer config with safe defaults.

    Worklink autonomy config is read by scheduler/poller loops; a malformed
    scalar should not crash the loop forever. Fall back to the dataclass default
    for non-int, bool, or non-positive values.
    """
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed <= 0:
        return default
    return parsed


def _normalize_compute_backend_name(name: str) -> str:
    return name.strip().replace("-", "_")


def _expect_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    return value


_TRUE_TOKENS = frozenset({"true", "1", "yes", "on"})
_FALSE_TOKENS = frozenset({"false", "0", "no", "off", ""})


def _coerce_safety_bool(value: Any, *, default: bool = False) -> bool:
    """Fail-closed bool coercion for safety knobs (e.g.
    ``allow_autonomous_local_subprocess``).

    Plain ``bool(value)`` is unsafe here: ``bool("false") is True`` and any
    non-empty string would silently enable the unsafe path. So accept real YAML
    booleans, 0/1 ints, and an explicit recognised true/false token set; anything
    unrecognised (a typo, an arbitrary string) returns ``default`` — i.e. stays
    OFF — rather than enabling the gate.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):  # YAML 0/1 only (bool already handled above)
        if value in (0, 1):
            return bool(value)
        return default
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _TRUE_TOKENS:
            return True
        if token in _FALSE_TOKENS:
            return False
    return default


def _parse_tool_pins(value: Any) -> tuple[ToolPin, ...]:
    if not isinstance(value, list):
        raise ValueError("worklink tool_pins must be a list")
    return tuple(_parse_tool_pin(item, index=index) for index, item in enumerate(value))


def _parse_tool_pin(value: Any, *, index: int) -> ToolPin:
    if not isinstance(value, dict):
        raise ValueError(f"worklink tool_pins[{index}] must be a mapping")
    missing = [field for field in ("name", "category", "pin", "smoke") if field not in value]
    if missing:
        raise ValueError(
            f"worklink tool_pins[{index}] missing required field(s): {', '.join(missing)}"
        )
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


def _parse_tiered_review_config(value: Any) -> TieredReviewConfig:
    defaults = TieredReviewConfig()
    if value is None:
        return defaults
    if not isinstance(value, dict):
        raise ValueError("worklink defaults.tiered_review must be a mapping")
    high_risk_scope_patterns = value.get("high_risk_scope_patterns")
    return TieredReviewConfig(
        high_risk_scope_patterns=_string_tuple_config(
            high_risk_scope_patterns,
            default=defaults.high_risk_scope_patterns,
            field_name="worklink defaults.tiered_review.high_risk_scope_patterns",
        ),
        high_risk_labels=_string_tuple_config(
            value.get("high_risk_labels"),
            default=defaults.high_risk_labels,
            field_name="worklink defaults.tiered_review.high_risk_labels",
        ),
        multi_vote_reviewer_count=_positive_int(
            value.get("multi_vote_reviewer_count"),
            default=defaults.multi_vote_reviewer_count,
        ),
    )


def _string_tuple_config(
    value: Any,
    *,
    default: tuple[str, ...],
    field_name: str,
) -> tuple[str, ...]:
    if value is None:
        return default
    if not isinstance(value, list | tuple):
        raise ValueError(f"{field_name} must be a list of strings")
    if not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be a list of strings")
    return tuple(value)


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
        compute_backend=(
            _normalize_compute_backend_name(str(value["compute_backend"]))
            if "compute_backend" in value
            else None
        ),
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
        for name, settings in self.config.compute_backend_settings.items():
            self._compute_backends[name] = self._build_compute_backend(name, settings)

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
        normalized = _normalize_compute_backend_name(name)
        try:
            return self._compute_backends[normalized]
        except KeyError as exc:
            raise KeyError(f"unknown Worklink compute backend: {name}") from exc

    def select_compute(
        self,
        *,
        labels: set[str] | None = None,
        repo: str | None = None,
        tool_category: str | None = None,
    ) -> ComputeBackend:
        name = self.config.select_compute_backend_name(
            labels=labels, repo=repo, tool_category=tool_category
        )
        try:
            return self.get_compute(name)
        except KeyError:
            if _normalize_compute_backend_name(name) == "docker_sibling":
                raise ValueError(
                    "worklink docker-sibling compute backend requires "
                    "compute_backends.docker-sibling config"
                ) from None
            raise

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
    def _build_compute_backend(name: str, settings: Mapping[str, Any]) -> ComputeBackend:
        if name == "local_subprocess":
            if settings:
                raise ValueError(
                    "worklink local-subprocess compute backend does not accept settings"
                )
            return LocalSubprocessComputeBackend()
        if name == "docker_sibling":
            allowed = {"broker_url", "broker_endpoint", "image", "policy"}
            unknown = sorted(set(settings) - allowed)
            if unknown:
                raise ValueError(
                    "worklink docker-sibling compute backend unknown setting(s): "
                    + ", ".join(unknown)
                )
            broker_url = str(settings.get("broker_url", settings.get("broker_endpoint", "")))
            image = str(settings.get("image", ""))
            policy = settings.get("policy") or {}
            if not isinstance(policy, Mapping):
                raise ValueError("worklink docker-sibling policy must be a mapping")
            return DockerSiblingComputeBackend(broker_url=broker_url, image=image, policy=policy)
        if name == "ecs_runtask":
            return BackendRegistry._build_ecs_runtask(settings)
        raise ValueError(f"unknown Worklink compute backend config: {name}")


    @staticmethod
    def _build_ecs_runtask(settings: Mapping[str, Any]) -> EcsRunTaskComputeBackend:
        allowed = {
            "cluster",
            "task_definition",
            "container_name",
            "subnets",
            "security_groups",
            "assign_public_ip",
            "launch_type",
            "platform_version",
            "task_role_arn",
            "execution_role_arn",
            "worker_repo_dir",
            "worker_evidence_path",
            "worker_transcript_root",
            "safe_env",
            "tags",
        }
        unknown = sorted(set(settings) - allowed)
        if unknown:
            raise ValueError(
                "worklink ecs_runtask compute backend unknown setting(s): "
                + ", ".join(unknown)
            )
        missing = [
            field
            for field in ("cluster", "task_definition", "container_name", "subnets")
            if field not in settings
        ]
        if missing:
            raise ValueError(f"worklink ecs_runtask missing required field(s): {', '.join(missing)}")
        subnets = _string_tuple(settings.get("subnets"), field_name="worklink ecs_runtask subnets")
        if not subnets:
            raise ValueError("worklink ecs_runtask subnets must not be empty")
        security_groups = _string_tuple(
            settings.get("security_groups", ()), field_name="worklink ecs_runtask security_groups"
        )
        safe_env = settings.get("safe_env") or {}
        if not isinstance(safe_env, dict):
            raise ValueError("worklink ecs_runtask safe_env must be a mapping")
        tags = settings.get("tags") or {}
        if not isinstance(tags, dict):
            raise ValueError("worklink ecs_runtask tags must be a mapping")
        config = EcsRunTaskConfig(
            cluster=str(settings["cluster"]),
            task_definition=str(settings["task_definition"]),
            container_name=str(settings["container_name"]),
            subnets=subnets,
            security_groups=security_groups,
            assign_public_ip=bool(settings.get("assign_public_ip", False)),
            launch_type=str(settings.get("launch_type", "FARGATE")),
            platform_version=(
                str(settings["platform_version"]) if "platform_version" in settings else None
            ),
            task_role_arn=(str(settings["task_role_arn"]) if "task_role_arn" in settings else None),
            execution_role_arn=(
                str(settings["execution_role_arn"]) if "execution_role_arn" in settings else None
            ),
            worker_repo_dir=str(settings.get("worker_repo_dir", "/worklink/repo")),
            worker_evidence_path=str(
                settings.get("worker_evidence_path", "/worklink/evidence/evidence.json")
            ),
            worker_transcript_root=(
                str(settings["worker_transcript_root"])
                if "worker_transcript_root" in settings
                else "/worklink/transcripts"
            ),
            safe_env={str(key): str(value) for key, value in safe_env.items()},
            tags={str(key): str(value) for key, value in tags.items()},
        )
        return EcsRunTaskComputeBackend(config)


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


def _string_tuple(value: Any, *, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        raise ValueError(f"{field_name} must be a list of strings")
    if not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be a list of strings")
    return tuple(value)
