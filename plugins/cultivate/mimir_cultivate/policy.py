"""Cultivate's owner-bound repo and pull-request action vocabulary."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time
from types import MappingProxyType
from typing import TYPE_CHECKING

from mimir.models import (
    RepoPRAction,  # Definition moves here in retire-worklink.
    RepoPRActionScope,  # Definition moves here in retire-worklink.
    RepoPRScopeProvenance,  # Definition moves here in retire-worklink.
    RepoPRScopeRegistry,  # Definition moves here in retire-worklink.
    RepoReviewState,  # Definition moves here in retire-worklink.
    RetainedFactoryScope,  # Definition moves here in retire-worklink.
    ServerDiscoveredPRScopeStore,  # Definition moves here in retire-worklink.
    ServerDiscoveredPRStates,  # Definition moves here in retire-worklink.
)
from mimir.tool_descriptors import (
    ResultOriginKind, SinkCategory, ToolDescriptor, _argument_payload, _generic_target,
)

if TYPE_CHECKING:
    from mimir.plugins.registry import PolicyRegistrar


@dataclass(frozen=True)
class CultivateState:
    """Owner-scoped turn state; these references alone confer no authority."""

    repo_pr_scope_registry: RepoPRScopeRegistry | None = None
    repo_review_state: RepoReviewState | None = None
    repo_pr_action_scope: RepoPRActionScope | None = None
    server_discovered_pr_states: ServerDiscoveredPRStates | None = None
    server_discovered_pr_scope_store: ServerDiscoveredPRScopeStore | None = None
    retained_factory_scope: RetainedFactoryScope | None = None
    retained_factory_scope_refusal: str | None = None
    ci_run_targets: frozenset[tuple[str, int]] = frozenset()
    ci_branch_targets: frozenset[tuple[str, str, int | None]] = frozenset()

    @classmethod
    def project_trusted_auth(cls, auth: object) -> CultivateState | None:
        """Copy only host-issued ingress fields, never plugin or tool arguments."""
        values = {
            name: object.__getattribute__(auth, name)
            for name in cls.__dataclass_fields__
        }
        service = object.__getattribute__(auth, "service_authority")
        if (
            object.__getattribute__(auth, "trigger") == "scheduled_tick"
            and getattr(service, "authority_profile", None) not in {"github", "heartbeat"}
            and values["repo_pr_scope_registry"] is None
            and values["repo_review_state"] is None
            and values["repo_pr_action_scope"] is None
            and not getattr(values["server_discovered_pr_states"], "review_states", ())
            and values["retained_factory_scope"] is None
            and not values["ci_run_targets"]
            and not values["ci_branch_targets"]
        ):
            return None
        return cls(**values)


def register_action_protocol(registrar: PolicyRegistrar, validate_scope: Callable[..., bool]) -> None:
    """Bind the vocabulary and grant validator to this selected registrar."""
    registrar.register_scoped_action(
        RepoPRAction, validate_scope, checkout_action=RepoPRAction.CHECKOUT,
        live_lease=_live_scoped_pr_lease,
    )


def _live_scoped_pr_lease(scope: object, review_state: object) -> object | None:
    attached = getattr(review_state, "checkout_lease", None)
    if attached is None or not getattr(attached, "is_active", False):
        return None
    try:
        root = Path(os.environ["MIMIR_PR_CHECKOUT_LEASE_ROOT"])
        if not root.is_absolute() or root.is_symlink():
            return None
        root = root.resolve(strict=True)
        path = Path(attached.path)
        if path.is_symlink() or path.resolve(strict=True).parent != root:
            return None
        metadata = path / ".git/mimir-pr-checkout-lease.json"
        if (metadata.is_symlink() or metadata.parent.is_symlink()
                or (path / ".git/mimir-pr-checkout-lease-reclamation.json").exists()):
            return None
        raw = json.loads(metadata.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        if any(raw.get(field) != str(getattr(attached, field)) for field in ("path", "lease_root")):
            return None
        if any(raw.get(field) != getattr(attached, field) for field in (
            "recovery_id", "scope_id", "owner", "head_sha", "canonical_origin",
            "canonical_repo", "pr_number", "destination_ref",
        )):
            return None
        expires = datetime.fromisoformat(raw["expires_at"])
        if expires.tzinfo is None or expires <= datetime.now(UTC) or expires != attached.expires_at:
            return None
    except (OSError, KeyError, ValueError, TypeError, AttributeError):
        return None
    if (attached.scope_id != scope.scope_id or attached.owner != scope.principal
            or attached.head_sha.lower() != scope.observed_head_sha.lower()
            or attached.canonical_repo.lower() != scope.canonical_repo.lower()
            or getattr(review_state, "action_scope", None) is not scope):
        return None
    if _observed_checkout_state(path) != (
        scope.head_ref, getattr(review_state, "git_expected_head", None)
    ):
        return None
    return attached


def _observed_checkout_state(path: Path) -> tuple[str, str] | None:
    """Inspect the current checkout without inheriting user Git configuration."""
    env = {
        "PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_ATTR_NOSYSTEM": "1", "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0",
    }
    output = bytearray()
    try:
        process = subprocess.Popen(
            ("/usr/bin/git", "-C", str(path), "-c", "core.fsmonitor=",
             "-c", "core.hooksPath=/dev/null", "-c", "core.pager=cat",
             "-c", "protocol.allow=never", "-c", "submodule.recurse=false",
             "--no-pager", "--no-optional-locks", "status", "--porcelain=v2",
             "--branch", "--untracked-files=no"),
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, close_fds=True, start_new_session=True,
        )
        try:
            assert process.stdout is not None and process.stderr is not None
            with selectors.DefaultSelector() as selector:
                for stream in (process.stdout, process.stderr):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ)
                deadline = time.monotonic() + 5
                remaining_bytes = 1_048_576
                while selector.get_map():
                    remaining_time = deadline - time.monotonic()
                    if remaining_time <= 0:
                        return None
                    for key, _ in selector.select(min(remaining_time, 0.1)):
                        stream = key.fileobj
                        chunk = os.read(stream.fileno(), min(65_536, remaining_bytes + 1))
                        if not chunk:
                            selector.unregister(stream)
                            continue
                        if len(chunk) > remaining_bytes:
                            return None
                        remaining_bytes -= len(chunk)
                        if stream is process.stdout:
                            output.extend(chunk)
                if process.wait(timeout=max(0, deadline - time.monotonic())) != 0:
                    return None
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait()
            process.stdout.close()
            process.stderr.close()
    except (OSError, subprocess.SubprocessError):
        return None
    fields = {}
    for line in output.decode("utf-8", errors="replace").splitlines():
        if line.startswith("# branch."):
            key, separator, value = line[2:].partition(" ")
            if separator:
                fields[key] = value
    branch, head = fields.get("branch.head"), fields.get("branch.oid", "").lower()
    if (not branch or branch == "(detached)" or len(head) != 40
            or any(char not in "0123456789abcdef" for char in head)):
        return None
    return branch, head


def validate_scoped_pr_action(action: RepoPRAction, scope: object, grant: object) -> bool:
    """Validate the selected owner's action against host-observed authority."""
    return (
        type(action) is RepoPRAction
        and action.value in getattr(scope, "allowed_operations", ())
        and getattr(grant, "scope_id", None) == getattr(scope, "scope_id", None)
        and getattr(grant, "owner", None) == "cultivate"
        and getattr(grant, "action", None) == action.value
    )


_RETAINED = ("repo_status", "repo_diff", "repo_test", "repo_stage", "repo_commit")
_BUILDS = ("worklink_run", "worklink_resume")
_PUBLISH = ("repo_commit", "repo_merge", "repo_rebase", "repo_revert", "repo_push")


def host_membership_projection() -> dict[str, tuple[str, ...]]:
    return {
        "retained_repo_tools": _RETAINED,
        "worklink_build_tools": _BUILDS,
        "repo_publish_tools": _PUBLISH,
    }


def resolve_review_state_for_context(auth_context: object, repository: object, pull_request: object) -> object:
    from .forge import resolve_review_state_for_context as resolve

    return resolve(auth_context, repository, pull_request)


def revalidate_review_head_for_context(auth_context: object, repository: object, pull_request: object) -> object:
    from .forge import revalidate_review_head_for_context as revalidate

    return revalidate(auth_context, repository, pull_request)


def _names(value: str) -> frozenset[str]:
    return frozenset(value.split())


_FORGE = _names("""pr_list pr_metadata pr_spec pr_files pr_diff pr_file_content
    pr_checks pr_job_log ci_run_jobs ci_run ci_recent_runs pr_reviews pr_comments
    pr_review_requests pr_submit_review pr_inline_review_comment pr_comment
    pr_edit_body issue_comment pr_rerequest_review unsupported_operation""")
_REPO = _names("""repo_checkout repo_cleanup repo_fetch repo_status repo_test
    repo_diff repo_unmerged repo_stage repo_commit repo_merge repo_merge_abort
    repo_rebase repo_rebase_abort repo_revert repo_revert_abort repo_push""")
CODING_TOOLS = _FORGE | _REPO | frozenset(_BUILDS) | {"spawn_open_code"}


def _spawn_target(_name: str, arguments: dict, _context: object) -> tuple[str | None, ...]:
    target = arguments.get("cwd") or os.environ.get("MIMIR_HOME")
    artifact = arguments.get("artifact_root")
    return (str(target) if target else None, str(artifact)) if artifact else (
        str(target) if target else None,
    )


def _worklink_target(_name: str, _arguments: dict, _context: object) -> tuple[str | None, ...]:
    target = os.environ.get("WORKLINK_REPO") or os.environ.get("MIMIR_WORKLINK_REPO")
    return (str(target) if target else None,)


def _repo_pr_target(tool_name: str, arguments: dict, context: object) -> tuple[str | None, ...]:
    from mimir.plugins.core import selected_policy_registry

    tool = selected_policy_registry().tools.get(tool_name)
    retained = getattr(context, "retained_factory_scope", None)
    if (tool is not None and tool.owner == "cultivate"
            and "retained_repo_tools" in tool.memberships
            and isinstance(retained, RetainedFactoryScope)
            and getattr(context, "is_service", False)
            and arguments.get("repository") == retained.repository
            and arguments.get("pull_request") == retained.issue_id):
        return (f"{retained.repository}#issue/{retained.issue_id}@retained/{retained.scope_id}",)
    repository, number = arguments.get("repository"), arguments.get("pull_request")
    discovered = getattr(context, "server_discovered_pr_states", None)
    state = (discovered.resolve_for_tool(tool_name, repository, number)
             if discovered is not None and isinstance(repository, str) and isinstance(number, int)
             else None)
    if state is None:
        registry = getattr(context, "repo_pr_scope_registry", None)
        state = (registry.resolve(repository, number)
                 if registry is not None and hasattr(registry, "resolve") else None)
    if state is None:
        return (None,)
    scope = state.action_scope
    return (f"{scope.canonical_repo}#pull/{scope.pr_number}"
            f"@{scope.observed_head_sha}:{scope.scope_id}",)


_D = ToolDescriptor
_E = ResultOriginKind.EXTERNAL
_R = ResultOriginKind.REPOSITORY
_N = ResultOriginKind.NON_INGESTING
_DESCRIPTORS = MappingProxyType({
    "ci_recent_runs": _D(result_origin=_E | _R),
    "ci_run": _D(result_origin=_E | _R),
    "ci_run_jobs": _D(result_origin=_E | _R),
    "issue_comment": _D(SinkCategory.FORGE, _generic_target, "configured_repository_issue", sink_payload_extractor=_argument_payload("body")),
    "pr_checks": _D(result_origin=_E | _R),
    "pr_comment": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_R, sink_payload_extractor=_argument_payload("body")),
    "pr_comments": _D(result_origin=_E | _R),
    "pr_diff": _D(result_origin=_E | _R),
    "pr_edit_body": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_R, sink_payload_extractor=_argument_payload("body")),
    "pr_file_content": _D(result_origin=_E | _R),
    "pr_files": _D(result_origin=_E | _R),
    "pr_inline_review_comment": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_R, sink_payload_extractor=_argument_payload("body")),
    "pr_job_log": _D(result_origin=_E | _R),
    "pr_list": _D(result_origin=_E | _R),
    "pr_metadata": _D(result_origin=_E | _R),
    "pr_rerequest_review": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_N, sink_payload_extractor=None),
    "pr_review_requests": _D(result_origin=_E | _R),
    "pr_reviews": _D(result_origin=_E | _R),
    "pr_spec": _D(result_origin=_R),
    "pr_submit_review": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_R, sink_payload_extractor=_argument_payload("body")),
    "repo_checkout": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_R, sink_payload_extractor=None),
    "repo_cleanup": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_N, sink_payload_extractor=None),
    "repo_commit": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_R, git_operation_result=True, sink_payload_extractor=_argument_payload("message")),
    "repo_diff": _D(result_origin=_R, git_operation_result=True),
    "repo_fetch": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_R, git_operation_result=True, sink_payload_extractor=None),
    "repo_merge": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_R, git_operation_result=True, sink_payload_extractor=None),
    "repo_merge_abort": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_R, git_operation_result=True, sink_payload_extractor=None),
    "repo_push": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_R, git_operation_result=True, sink_payload_extractor=None),
    "repo_rebase": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_R, git_operation_result=True, sink_payload_extractor=None),
    "repo_rebase_abort": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_R, git_operation_result=True, sink_payload_extractor=None),
    "repo_revert": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_R, git_operation_result=True, sink_payload_extractor=None),
    "repo_revert_abort": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_R, git_operation_result=True, sink_payload_extractor=None),
    "repo_stage": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_N, git_operation_result=True, sink_payload_extractor=None),
    "repo_status": _D(result_origin=_R, git_operation_result=True),
    "repo_test": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_R, sink_payload_extractor=None),
    "repo_unmerged": _D(result_origin=_R, git_operation_result=True),
    "spawn_open_code": _D(SinkCategory.SPAWN, _spawn_target, "spawn_process", ifc_delegation=True),
    "unsupported_operation": _D(SinkCategory.FORGE, _repo_pr_target, "bound_pull_request", result_origin=_N, sink_payload_extractor=None),
    "worklink_resume": _D(SinkCategory.SPAWN, _worklink_target, "worklink"),
    "worklink_run": _D(SinkCategory.SPAWN, _worklink_target, "worklink"),
})

_FLOW = MappingProxyType({
    "source": _names("""pr_list pr_metadata pr_spec pr_files pr_diff pr_file_content pr_checks
        pr_job_log ci_run_jobs ci_run ci_recent_runs pr_reviews pr_comments pr_review_requests
        repo_status repo_diff repo_unmerged"""),
    "both": _names("worklink_run worklink_resume spawn_open_code repo_checkout repo_fetch repo_test"),
    "sink": _names("""pr_submit_review pr_inline_review_comment pr_comment pr_edit_body issue_comment
        pr_rerequest_review unsupported_operation repo_cleanup repo_stage repo_commit repo_merge
        repo_merge_abort repo_rebase repo_rebase_abort repo_revert repo_revert_abort repo_push"""),
})
_TIER = MappingProxyType({
    "scope-contained": _names("""pr_list pr_metadata pr_spec pr_files pr_diff pr_file_content
        pr_checks pr_job_log ci_run_jobs ci_run ci_recent_runs pr_reviews pr_comments
        pr_review_requests repo_status repo_diff repo_unmerged"""),
    "code-execution": _names("worklink_run worklink_resume spawn_open_code repo_test"),
    "scoped-with-provenance": _names("""pr_submit_review pr_inline_review_comment pr_comment
        pr_edit_body issue_comment pr_rerequest_review unsupported_operation repo_checkout repo_cleanup
        repo_fetch repo_stage repo_commit repo_merge repo_merge_abort repo_rebase repo_rebase_abort
        repo_revert repo_revert_abort repo_push"""),
})
_RESULT_DOMAIN = MappingProxyType({
    "worklink": frozenset(_BUILDS), "coding_worker": _names("spawn_open_code"),
    "repository": _names("""pr_list pr_metadata pr_spec pr_files pr_diff pr_file_content pr_checks
        pr_job_log ci_run_jobs ci_run ci_recent_runs pr_reviews pr_comments pr_review_requests
        pr_submit_review pr_inline_review_comment pr_comment pr_edit_body issue_comment
        repo_checkout repo_fetch repo_status repo_test repo_diff repo_unmerged repo_commit
        repo_merge repo_merge_abort repo_rebase repo_rebase_abort repo_revert repo_revert_abort repo_push"""),
})
_READABLE_REPOSITORY = _names("""pr_list pr_metadata pr_spec pr_files pr_diff pr_file_content
    pr_checks pr_job_log ci_run_jobs ci_run ci_recent_runs pr_reviews pr_comments
    pr_review_requests repo_checkout repo_fetch repo_status repo_test repo_diff repo_unmerged""")
_READ_BACKEND = _names("""pr_metadata pr_spec pr_list pr_files pr_diff pr_file_content pr_checks
    pr_job_log ci_run_jobs ci_run ci_recent_runs pr_reviews pr_comments pr_review_requests
    repo_checkout repo_fetch repo_status repo_test repo_diff repo_unmerged""")
_HEARTBEAT = _names("""worklink_run pr_list pr_metadata pr_spec pr_files pr_diff pr_file_content
    pr_checks pr_reviews pr_comments pr_review_requests pr_submit_review pr_inline_review_comment
    pr_comment pr_edit_body pr_rerequest_review unsupported_operation repo_checkout repo_cleanup
    repo_fetch repo_status repo_test repo_diff repo_unmerged repo_stage repo_commit repo_merge
    repo_merge_abort repo_rebase repo_rebase_abort repo_revert repo_revert_abort repo_push""")
_WRITES = _names("""spawn_open_code worklink_run worklink_resume repo_checkout repo_cleanup
    repo_fetch repo_test repo_stage repo_commit repo_merge repo_merge_abort repo_rebase
    repo_rebase_abort repo_revert repo_revert_abort repo_push pr_submit_review
    pr_inline_review_comment pr_comment pr_edit_body issue_comment pr_rerequest_review""")
_MEMBERSHIPS = MappingProxyType({
    "write_resource_operations": frozenset(_BUILDS),
    "tainted_writes": _names("repo_stage repo_commit repo_merge repo_rebase repo_revert repo_push repo_test"),
    "standing_review": _names("""pr_metadata pr_spec pr_files pr_diff pr_file_content pr_checks
        pr_reviews pr_comments pr_review_requests pr_submit_review pr_inline_review_comment
        pr_comment pr_edit_body pr_rerequest_review repo_checkout repo_cleanup repo_fetch
        repo_status repo_test repo_diff repo_unmerged"""),
    "remediation_effect": _names("""repo_commit repo_push pr_comment pr_edit_body
        pr_inline_review_comment pr_rerequest_review worklink_resume"""),
    "typed_error_signal": _names("worklink_run"),
    "forge_actions": _FORGE - _names("pr_list ci_run_jobs ci_run ci_recent_runs issue_comment"),
    "repo_actions": _REPO,
})
_ACTIONS = MappingProxyType({
    **{name: RepoPRAction.INSPECT for name in _names("""pr_metadata pr_spec pr_files
        pr_diff pr_file_content pr_checks pr_job_log pr_reviews pr_comments pr_review_requests
        repo_status repo_diff repo_unmerged""")},
    **{name: RepoPRAction.CHECKOUT for name in _names("repo_checkout repo_cleanup repo_fetch")},
    "repo_test": RepoPRAction.TEST,
    **{name: RepoPRAction.WRITE for name in _names("repo_stage repo_merge_abort repo_rebase_abort repo_revert_abort")},
    **{name: RepoPRAction.COMMIT for name in _names("repo_commit repo_merge repo_rebase repo_revert")},
    "repo_push": RepoPRAction.PUSH,
    "pr_submit_review": RepoPRAction.PR_REVIEW,
    "pr_inline_review_comment": RepoPRAction.PR_REVIEW,
    "pr_comment": RepoPRAction.PR_COMMENT,
    "pr_edit_body": RepoPRAction.PR_EDIT,
    "pr_rerequest_review": RepoPRAction.PR_REREQUEST,
})


@dataclass(frozen=True)
class _ToolDeclaration:
    descriptor: ToolDescriptor
    flow: str
    tier: str
    operation_class: str
    result_domain: str | None
    readable_domain: str | None
    profiles: frozenset[str]
    memberships: frozenset[str]
    read_backend: bool
    writes_code_or_state: bool
    typed_action: RepoPRAction | None


def _declaration(name: str, descriptor: ToolDescriptor) -> _ToolDeclaration:
    return _ToolDeclaration(
        descriptor=descriptor,
        flow=next(flow for flow, names in _FLOW.items() if name in names),
        tier=next(tier for tier, names in _TIER.items() if name in names),
        operation_class="admin_required" if name in {"pr_list", "issue_comment", "spawn_open_code"} else "resource_scoped",
        result_domain=next((domain for domain, names in _RESULT_DOMAIN.items() if name in names), None),
        readable_domain="repository" if name in _READABLE_REPOSITORY else None,
        profiles=frozenset(profile for profile, names in {
            "github": CODING_TOOLS - {"spawn_open_code"},
            "custom": CODING_TOOLS - {"issue_comment"},
            "heartbeat": _HEARTBEAT,
        }.items() if name in names),
        memberships=frozenset(category for category, names in _MEMBERSHIPS.items() if name in names),
        read_backend=name in _READ_BACKEND,
        writes_code_or_state=name in _WRITES,
        typed_action=_ACTIONS.get(name),
    )


_TOOL_DECLARATIONS = MappingProxyType({
    name: _declaration(name, descriptor) for name, descriptor in _DESCRIPTORS.items()
})
if set(_TOOL_DECLARATIONS) != CODING_TOOLS or any(
    sum(name in group for group in table.values()) != 1
    for name in CODING_TOOLS for table in (_FLOW, _TIER)
):
    raise RuntimeError("incomplete Cultivate tool policy")


def _factory(module: str, name: str) -> Callable[[], object]:
    def materialize() -> object:
        from importlib import import_module

        source = import_module(module)
        if name == "spawn_open_code":
            return source.spawn_open_code
        if name in _BUILDS:
            return getattr(source, name)
        collection = source.FORGE_TOOLS if module.endswith("forge") else source.REPO_TOOLS
        return next(tool for tool in collection if tool.name == name)

    return materialize


def register_tools(registrar: PolicyRegistrar) -> None:
    """Install the moved tool implementations on the selected policy before freeze."""
    from mimir.plugins.registry import PolicyConsumerHooks
    from . import forge, repository, review_guard
    from .worklink.retained_scope import derive_retained_factory_scope

    registrar.register_policy_consumer_hooks(PolicyConsumerHooks(
        owner="cultivate", resolve_review=forge.resolve_review_state_for_context,
        revalidate_head=forge.revalidate_review_head_for_context,
        resolve_issue=forge.resolve_issue_comment_target,
        superseded_push=repository.was_superseded_by_own_push,
        derive_retained_scope=derive_retained_factory_scope,
        active_checkout=repository.active_pr_checkout_lease_for_path,
        git_command=repository.hardened_git_command,
        protected_refs=repository._PROTECTED_BRANCH_REFS,
        lineage=repository._recorded_lineage,
        verified_base=repository._recorded_verified_base,
        git_binary=repository._DEFAULT_GIT, git_config=repository._BASE_CONFIG,
        git_env=repository._sanitized_git_env, lease_metadata=repository._METADATA,
        review_submission=review_guard.review_submission_from_request,
        claim_review=review_guard.claim_review_submission,
    ))
    for name, declaration in sorted(_TOOL_DECLARATIONS.items()):
        descriptor = declaration.descriptor
        module = (
            "mimir_cultivate.forge" if name.startswith(("pr_", "ci_", "issue_"))
            or name == "unsupported_operation" else
            "mimir_cultivate.repository" if name.startswith("repo_") else
            "mimir_cultivate.registration" if name in _BUILDS else
            "mimir.tools.registry"
        )
        action = declaration.typed_action
        adapter = (
            "worklink_repo" if name in _BUILDS else
            "spawn_workspace" if descriptor.sink_category == SinkCategory.SPAWN else
            None
        )
        registrar.register_tool(
            name=name, factory=_factory(module, name), flow=declaration.flow,
            tier=declaration.tier, operation_class=declaration.operation_class,
            resource_scope=(
                "worklink" if name in _BUILDS else
                "ci_branch" if name == "ci_recent_runs" else
                "ci_run" if name in {"ci_run", "ci_run_jobs"} else
                "repo_pr" if declaration.operation_class == "resource_scoped" and action is not None
                else "channel" if declaration.operation_class == "resource_scoped" else None
            ),
            typed_action=action, result_domain=declaration.result_domain,
            result_origin=descriptor.result_origin,
            sink_category=descriptor.sink_category,
            sink_destination=descriptor.sink_destination,
            sink_target_extractor=descriptor.sink_target_extractor,
            sink_payload_extractor=descriptor.sink_payload_extractor,
            fetch_authorization=descriptor.fetch_authorization,
            git_operation_result=descriptor.git_operation_result,
            ifc_delegation=descriptor.ifc_delegation,
            budget_exempt=descriptor.budget_exempt,
            writes_code_or_state=declaration.writes_code_or_state,
            profiles=declaration.profiles, read_backend=declaration.read_backend,
            readable_domain=declaration.readable_domain,
            memberships=declaration.memberships, service_sink_adapter=adapter,
        )
        registrar.register_capability(name)
        for profile in declaration.profiles:
            registrar.add_to_profile(profile, name)
