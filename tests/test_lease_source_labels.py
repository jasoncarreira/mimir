from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from mimir import access_control as access_control_module
from mimir.access_control import (
    OperationDecision,
    SinkGate,
    ToolAuthorization,
    begin_protected_result_capture,
    classify_protected_result,
    end_protected_result_capture,
    protected_result_source,
)
from mimir.models import (
    AuthContext,
    InformationFlowLabels,
    InformationFlowState,
    RepoPRActionScope,
    RepoReviewState,
    SourceLabel,
    TurnInteractivity,
)
from mimir.pr_checkout_lease import active_pr_checkout_lease_for_path


@pytest.fixture(autouse=True)
def _self_login(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MIMIR_GITHUB_SELF_LOGIN", "mimir-bot")
    real_head_check = access_control_module._lease_head_is_author_attested
    # Most tests in this module exercise scope/lease binding with synthetic Git
    # metadata. Dedicated tests below exercise the real checkout-head predicate.
    monkeypatch.setattr(
        access_control_module, "_lease_head_is_author_attested",
        lambda *args, **kwargs: True,
    )
    yield real_head_check



def _remember_inventory(lease, scope):
    """Mirror the runner: capture the lease's test inventory before classification."""
    from mimir.project_tests import pytest_node_inventory, remember_node_inventory

    remember_node_inventory(lease.path, scope.scope_id, pytest_node_inventory(lease.path))


def _recorded(lease, scope):
    from mimir.project_tests import recorded_node_inventory

    return recorded_node_inventory(lease.path, scope.scope_id)

def _recorded_lease(
    root: Path,
    *,
    repository: str = "owner/repo",
    expires_at: datetime | None = None,
    scope: RepoPRActionScope | None = None,
) -> tuple[Path, Path, dict[str, object]]:
    checkout = root / "scope-lease"
    git_directory = checkout / ".git"
    git_directory.mkdir(parents=True)
    target = checkout / "src" / "work.py"
    target.parent.mkdir()
    target.write_text("work product\n", encoding="utf-8")
    now = datetime.now(UTC)
    record: dict[str, object] = {
        "version": 3,
        "canonical_repo": repository,
        "canonical_origin": f"https://github.com/{repository}.git",
        "source_root": str(root.parent / "source"),
        "scope_base_sha": "b" * 40,
        "base_sha": "b" * 40,
        "head_sha": "a" * 40,
        "destination_ref": "refs/heads/worklink/7",
        "owner": "mimir-bot",
        "scope_id": (scope or _scope(repository)).scope_id,
        "path": str(checkout),
        "lease_root": str(root),
        "created_at": now.isoformat(),
        "expires_at": (expires_at or now + timedelta(hours=1)).isoformat(),
        "recovery_id": "recovery-id",
        "pr_number": 7,
    }
    (git_directory / "mimir-pr-checkout-lease.json").write_text(
        json.dumps(record), encoding="utf-8",
    )
    return checkout, target, record


def _scope(
    repository: str = "owner/repo",
    *,
    pr_number: int = 7,
    observed_head_sha: str = "a" * 40,
    author: str = "mimir-bot",
) -> RepoPRActionScope:
    return RepoPRActionScope(
        provenance="server_discovered",
        canonical_repo=repository,
        canonical_root="/srv/source",
        canonical_origin=f"https://github.com/{repository}.git",
        principal="mimir-bot",
        event_type="pr_review",
        allowed_operations=frozenset({"repo.push"}),
        pr_number=pr_number,
        head_repo=repository,
        head_remote="origin",
        destination_ref="refs/heads/worklink/7",
        observed_head_sha=observed_head_sha,
        base_ref="main",
        observed_base_sha="b" * 40,
        pull_request_author=author,
    )


def _auth(
    labels: InformationFlowLabels | None = None,
    *,
    repository: str = "owner/repo",
    pr_number: int = 7,
    observed_head_sha: str = "a" * 40,
    scope: RepoPRActionScope | None = None,
    recorded_verdict: bool = True,
) -> AuthContext:
    current = labels or InformationFlowLabels()
    scope = scope or _scope(
        repository, pr_number=pr_number, observed_head_sha=observed_head_sha,
    )
    state = InformationFlowState(labels=current)
    if recorded_verdict:
        state.pr_checkout_author_trust[scope.scope_id] = True
    return AuthContext(
        principal="operator",
        canonical_principal="operator",
        roles=("user",),
        event_ingress=None,
        trigger="user_message",
        channel_id="channel-1",
        interactivity=TurnInteractivity.INTERACTIVE,
        enforcement_enabled=True,
        ifc_labels=current,
        ifc_state=state,
        repo_pr_action_scope=scope,
    )


def _real_attested_lease(tmp_path: Path):
    from mimir.git_bootstrap import DEFAULT_USER_EMAIL, DEFAULT_USER_NAME

    checkout = tmp_path / "checkout"
    subprocess.run(
        ["git", "init", "-q", "-b", "worklink/7", str(checkout)], check=True,
    )
    target = checkout / "work.py"
    target.write_text("attested\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(checkout), "add", "work.py"], check=True)
    subprocess.run([
        "git", "-C", str(checkout),
        "-c", "user.name=collaborator", "-c", "user.email=collaborator@example.test",
        "commit", "-qm", "attested head",
    ], check=True)
    head = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    scope = _scope(observed_head_sha=head, author="collaborator")
    lease = SimpleNamespace(
        path=checkout, scope_id=scope.scope_id, canonical_repo=scope.canonical_repo,
        pr_number=scope.pr_number, head_sha=head, owner=scope.principal,
        is_active=True,
    )
    auth = _auth(scope=scope)
    state = RepoReviewState(scope)
    state.attach_checkout_lease(lease)
    object.__setattr__(auth, "repo_review_state", state)
    return auth, scope, lease, target, (DEFAULT_USER_NAME, DEFAULT_USER_EMAIL)


def test_attested_lease_head_accepts_only_server_identity_descendants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _self_login,
) -> None:
    real_head_check = _self_login
    monkeypatch.setattr(
        access_control_module, "_lease_head_is_author_attested", real_head_check,
    )
    auth, scope, lease, target, server_identity = _real_attested_lease(tmp_path)

    assert access_control_module._attested_pr_checkout_lease(auth, scope, lease)

    target.write_text("server remediation\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(lease.path), "add", "work.py"], check=True)
    subprocess.run([
        "git", "-C", str(lease.path),
        "-c", f"user.name={server_identity[0]}",
        "-c", f"user.email={server_identity[1]}",
        "commit", "-qm", "server remediation",
    ], check=True)
    server_head = subprocess.run(
        ["git", "-C", str(lease.path), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    state = auth.repo_review_state
    assert state is not None
    state.record_git_head(scope.scope_id, server_head)
    assert access_control_module._attested_pr_checkout_lease(auth, scope, lease)

    target.write_text("foreign commit\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(lease.path), "add", "work.py"], check=True)
    subprocess.run([
        "git", "-C", str(lease.path),
        "-c", "user.name=foreign", "-c", "user.email=foreign@example.test",
        "commit", "-qm", "foreign commit",
    ], check=True)
    foreign_head = subprocess.run(
        ["git", "-C", str(lease.path), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    state.record_git_head(scope.scope_id, foreign_head)
    assert not access_control_module._attested_pr_checkout_lease(auth, scope, lease)


def test_attested_lease_verdict_is_cached_until_checkout_head_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth, scope, lease, target, server_identity = _real_attested_lease(tmp_path)
    state = auth.repo_review_state
    assert state is not None
    calls = []

    def verify(*args, **kwargs):
        calls.append((args, kwargs))
        return True

    monkeypatch.setattr(
        access_control_module, "_lease_head_is_author_attested", verify,
    )

    assert access_control_module._attested_pr_checkout_lease(auth, scope, lease)
    assert access_control_module._attested_pr_checkout_lease(auth, scope, lease)
    assert len(calls) == 1

    target.write_text("server remediation\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(lease.path), "add", "work.py"], check=True)
    subprocess.run([
        "git", "-C", str(lease.path),
        "-c", f"user.name={server_identity[0]}",
        "-c", f"user.email={server_identity[1]}",
        "commit", "-qm", "server remediation",
    ], check=True)
    server_head = subprocess.run(
        ["git", "-C", str(lease.path), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    state.record_git_head(scope.scope_id, server_head)

    assert access_control_module._attested_pr_checkout_lease(auth, scope, lease)
    assert len(calls) == 2


def test_attested_lease_cached_verdict_rejects_unrecorded_foreign_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _self_login,
) -> None:
    monkeypatch.setattr(
        access_control_module, "_lease_head_is_author_attested", _self_login,
    )
    auth, scope, lease, target, _identity = _real_attested_lease(tmp_path)

    assert access_control_module._attested_pr_checkout_lease(auth, scope, lease)

    target.write_text("foreign commit\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(lease.path), "add", "work.py"], check=True)
    subprocess.run([
        "git", "-C", str(lease.path),
        "-c", "user.name=foreign", "-c", "user.email=foreign@example.test",
        "commit", "-qm", "foreign commit",
    ], check=True)

    assert not access_control_module._attested_pr_checkout_lease(auth, scope, lease)


def test_attested_lease_rejects_unrecorded_server_identity_commit(
    tmp_path: Path,
) -> None:
    auth, scope, lease, target, server_identity = _real_attested_lease(tmp_path)

    assert access_control_module._attested_pr_checkout_lease(auth, scope, lease)

    target.write_text("unrecorded server remediation\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(lease.path), "add", "work.py"], check=True)
    subprocess.run([
        "git", "-C", str(lease.path),
        "-c", f"user.name={server_identity[0]}",
        "-c", f"user.email={server_identity[1]}",
        "commit", "-qm", "unrecorded server remediation",
    ], check=True)

    assert not access_control_module._attested_pr_checkout_lease(auth, scope, lease)


def test_attested_lease_rejects_checkout_observation_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth, scope, lease, _target, _identity = _real_attested_lease(tmp_path)
    monkeypatch.setattr(access_control_module, "_observed_checkout_state", lambda _: None)

    assert not access_control_module._attested_pr_checkout_lease(auth, scope, lease)


def test_attested_lease_head_rejects_foreign_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _self_login,
) -> None:
    monkeypatch.setattr(
        access_control_module, "_lease_head_is_author_attested", _self_login,
    )
    auth, scope, lease, _target, _identity = _real_attested_lease(tmp_path)
    subprocess.run(
        ["git", "-C", str(lease.path), "checkout", "-q", "-b", "foreign"], check=True,
    )

    assert not access_control_module._attested_pr_checkout_lease(auth, scope, lease)


@pytest.mark.parametrize("tool_name", ["repo_status", "repo_diff", "repo_test"])
def test_repo_result_inherits_matching_attested_lease(
    tool_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _self_login,
) -> None:
    from mimir.tools.repo import _publish_attested_lease_result

    monkeypatch.setattr(
        access_control_module, "_lease_head_is_author_attested", _self_login,
    )
    auth, scope, lease, _target, _identity = _real_attested_lease(tmp_path)
    state = RepoReviewState(scope)
    state.attach_checkout_lease(lease)
    token = begin_protected_result_capture()
    try:
        _publish_attested_lease_result(SimpleNamespace(context=auth), state)
    finally:
        provenance = end_protected_result_capture(token)

    labels = classify_protected_result(
        tool_name, {"repository": "owner/repo", "pull_request": 7}, auth,
        ToolAuthorization(
            tool_name=tool_name, decision=OperationDecision.RESOURCE_SCOPED,
            allowed=True, repo_pr_action_scope=scope,
        ),
        result="repository output", provenance=provenance,
    )

    assert labels is not None
    source, = labels.sources
    assert (source.integrity, source.integrity_effect) == ("trusted", "active_ingest")
    auth.ifc_state.merge(labels)
    assert not auth.ifc_state.has_untrusted_active_ingest()


def test_repo_result_without_author_verdict_stays_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _self_login,
) -> None:
    from mimir.tools.repo import _publish_attested_lease_result

    monkeypatch.setattr(
        access_control_module, "_lease_head_is_author_attested", _self_login,
    )
    auth, scope, lease, _target, _identity = _real_attested_lease(tmp_path)
    auth.ifc_state.pr_checkout_author_trust[scope.scope_id] = False
    state = RepoReviewState(scope)
    state.attach_checkout_lease(lease)
    token = begin_protected_result_capture()
    try:
        _publish_attested_lease_result(SimpleNamespace(context=auth), state)
    finally:
        provenance = end_protected_result_capture(token)

    assert provenance is None
    labels = classify_protected_result(
        "repo_status", {"repository": "owner/repo", "pull_request": 7}, auth,
        ToolAuthorization(
            tool_name="repo_status", decision=OperationDecision.RESOURCE_SCOPED,
            allowed=True, repo_pr_action_scope=scope,
        ),
        result="repository output", provenance=provenance,
    )
    assert labels is not None
    source, = labels.sources
    assert source.integrity == "untrusted"
    auth.ifc_state.merge(labels)
    assert auth.ifc_state.has_untrusted_active_ingest()


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict,include_output,second_allowed", [
    (True, False, True), (True, True, False), (False, False, False),
    (None, False, False),
])
@pytest.mark.parametrize("state_carrier", ["alias", "discovered", "reminted"])
async def test_repo_test_red_run_remediation_sequence(
    tmp_path, monkeypatch, _self_login, verdict, include_output, second_allowed, state_carrier,
):
    from mimir.project_tests import ProjectTestResult, pytest_failure_summary
    from mimir.tools import repo

    monkeypatch.setattr(access_control_module, "_lease_head_is_author_attested", _self_login)
    auth, scope, lease, _, _ = _real_attested_lease(tmp_path)
    auth.ifc_state.pr_checkout_author_trust[scope.scope_id] = verdict
    state = auth.repo_review_state
    test_file = lease.path / "tests" / "test_work.py"
    test_file.parent.mkdir()
    test_file.write_text("def test_fix():\n    pass\n")
    _remember_inventory(lease, scope)
    monkeypatch.setattr(repo, "_state", lambda *_: state)
    output = b"FAILED tests/test_work.py::test_fix - AssertionError\n=== short test summary info ===\nFAILED tests/test_work.py::test_fix - AssertionError\n=== 1 failed, 2 passed in 0.1s ===\n"

    async def execute(self, selectors, *, suite):
        return ProjectTestResult(False, "tests_failed", 1, stdout=output.decode(),
                                 stderr="untrusted stderr", git_context="git context",
                                 failure_summary=pytest_failure_summary(output, frozenset({"tests/test_work.py::test_fix"})))

    monkeypatch.setattr(repo.RepoProjectTests, "execute", execute)
    capture = begin_protected_result_capture()
    try:
        result = await repo.repo_test.coroutine(
            "owner/repo", 7, runtime=SimpleNamespace(context=auth),
            include_output=include_output,
        )
    finally:
        provenance = end_protected_result_capture(capture)
    if not include_output:
        assert set(result) == {"ok", "code", "exit_code", "suite", "selectors", "summary", "remediation_guidance"}
        assert result["summary"]["failing"] == ["tests/test_work.py::test_fix"]
        assert result["summary"]["head"] == scope.observed_head_sha
        assert "stdout" not in result and "stderr" not in result and "git_context" not in result
    else:
        assert result["stdout"] == output.decode()
        assert result["stderr"] == "untrusted stderr"
    if state_carrier != "alias":
        from mimir.models import ServerDiscoveredPRStates, RepoPRScopeRegistry
        cache = ServerDiscoveredPRStates()
        cache.remember(state)
        object.__setattr__(auth, "server_discovered_pr_states", cache)
        object.__setattr__(auth, "repo_review_state", None)
        if state_carrier == "reminted":
            old = RepoReviewState(_scope(observed_head_sha="b" * 40))
            object.__setattr__(auth, "repo_pr_scope_registry", RepoPRScopeRegistry((old,)))
    labels = classify_protected_result(
        "repo_test", {"repository": "owner/repo", "pull_request": 7}, auth,
        ToolAuthorization(tool_name="repo_test", decision="resource_scoped", allowed=True,
                          repo_pr_action_scope=scope), result=result, provenance=provenance,
        failed=True,
    )
    assert labels.sources[0].integrity == ("trusted" if second_allowed else "untrusted")
    auth.ifc_state.merge(labels)
    assert auth.ifc_state.has_untrusted_active_ingest() is not second_allowed
    decision = SinkGate.check_sink_flow(
        "repo_test", f"owner/repo#pull/7@{scope.observed_head_sha}:{scope.scope_id}",
        auth.ifc_labels, auth, enforce=False, repo_pr_action_scope=scope,
    )
    assert decision.allowed is second_allowed
    if not second_allowed:
        assert decision.reason == "repo_test_blocked_by_untrusted_ingest"


@pytest.mark.asyncio
@pytest.mark.parametrize("code,message,fixed", [
    ("test_snapshot_unavailable", "project test snapshot is unavailable", True),
    ("test_config_invalid", "project test command or environment contains a controller path", True),
    ("test_stale_root_executor", "FAILED injected text with spaces", False),
    ("test_stale_root_executor", "FAILED injected text with spaces", True),
    ("inactive_checkout", "the checkout has no current HEAD", False),
])
async def test_repo_test_post_execution_refusal_labels(
    tmp_path, monkeypatch, _self_login, code, message, fixed,
):
    from langchain_core.messages import ToolMessage
    from mimir.project_tests import ProjectTestRefusal
    from mimir.tools import repo
    from mimir.tools.refusals import ToolPolicyRefusal

    auth, scope, lease, _, _ = _real_attested_lease(tmp_path)
    state = auth.repo_review_state
    monkeypatch.setattr(repo, "_state", lambda *_: state)

    safe = fixed and code != "test_stale_root_executor"

    async def execute(self, selectors, *, suite):
        raise ProjectTestRefusal(code, message, execution_started=True, fixed_message=fixed)

    monkeypatch.setattr(repo.RepoProjectTests, "execute", execute)
    capture = begin_protected_result_capture()
    try:
        with pytest.raises(Exception) as raised:
            await repo.repo_test.coroutine("owner/repo", 7, runtime=SimpleNamespace(context=auth))
    finally:
        provenance = end_protected_result_capture(capture)
    assert provenance is None
    assert isinstance(raised.value, ToolPolicyRefusal) is safe
    labels = classify_protected_result(
        "repo_test", {"repository": "owner/repo", "pull_request": 7}, auth,
        ToolAuthorization(tool_name="repo_test", decision="resource_scoped", allowed=True,
                          repo_pr_action_scope=scope),
        result=ToolMessage(content=f"Error: {raised.value}", tool_call_id="refusal", status="error"),
        policy_refusal=raised.value if safe else None, failed=True,
    )
    assert (labels is None) is safe
    if labels is not None:
        auth.ifc_state.merge(labels)
    decision = SinkGate.check_sink_flow(
        "repo_test", f"owner/repo#pull/7@{scope.observed_head_sha}:{scope.scope_id}",
        auth.ifc_labels, auth, enforce=False, repo_pr_action_scope=scope,
    )
    assert decision.allowed is safe


@pytest.mark.asyncio
async def test_missing_selector_is_pre_execution_policy_refusal_and_allows_rerun(
    tmp_path, monkeypatch,
):
    from mimir import project_tests
    from mimir.tools import repo
    from mimir.tools.refusals import ToolPolicyRefusal

    auth, scope, lease, _, _ = _real_attested_lease(tmp_path)
    state = auth.repo_review_state
    monkeypatch.setattr(repo, "_state", lambda *_: state)

    class Git:
        execution_started = False

        def __init__(self, *_args):
            pass

        def validated_checkout_root(self):
            return lease.path

    monkeypatch.setattr(project_tests, "RepoGitTools", Git)
    object.__setattr__(scope, "allowed_operations", frozenset({"repo.test"}))
    monkeypatch.setattr(project_tests, "_configured_command", lambda *_args: (
        ("/usr/bin/true",), {}, "deployment", "default", True,
    ))
    with pytest.raises(ToolPolicyRefusal, match="test_selector_not_found"):
        await repo.repo_test.coroutine(
            "owner/repo", 7, selectors=("missing.py::test_case",),
            runtime=SimpleNamespace(context=auth),
        )
    assert SinkGate.check_sink_flow(
        "repo_test", f"owner/repo#pull/7@{scope.observed_head_sha}:{scope.scope_id}",
        auth.ifc_labels, auth, enforce=False, repo_pr_action_scope=scope,
    ).allowed


@pytest.mark.parametrize("tool_name", ["repo_fetch", "repo_push", "pr_metadata", "pr_files",
    "pr_diff", "pr_file_content", "pr_job_log", "pr_checks", "pr_reviews", "pr_comments", "pr_review_requests", "pr_list",
    "pr_submit_review", "pr_inline_review_comment", "pr_comment", "pr_edit_body"])
def test_other_failed_repository_results_never_use_attested_provenance(tool_name):
    auth = _auth()
    scope = auth.repo_pr_action_scope
    source = SourceLabel(principal="operator", domain="repository",
                         resource_id=f"owner/repo#pull/7@{scope.observed_head_sha}",
                         bridge_instance="forge", sensitivity="internal",
                         authorized_principals=frozenset({"operator"}),
                         source_kind="protected_tool", integrity="trusted",
                         integrity_effect="active_ingest")
    from mimir.access_control import ProtectedResultProvenance
    authorization = ToolAuthorization(tool_name=tool_name, decision="resource_scoped", allowed=True,
                                      repo_pr_action_scope=scope)
    arguments = {"repository": "owner/repo", "pull_request": 7}
    result = {"ok": False, "code": "tests_failed", "summary": {}}
    baseline = classify_protected_result(
        tool_name, arguments, auth, authorization, result=result, failed=True,
    )
    labels = classify_protected_result(
        tool_name, arguments, auth, authorization, result=result,
        provenance=ProtectedResultProvenance((source,)), failed=True,
    )
    assert labels == baseline


def test_failed_pr_rerequest_review_keeps_native_non_repository_labelling():
    from mimir.access_control import ProtectedResultProvenance

    auth = _auth()
    scope = auth.repo_pr_action_scope
    source = SourceLabel(principal="operator", domain="repository",
                         resource_id=f"owner/repo#pull/7@{scope.observed_head_sha}",
                         bridge_instance="forge", sensitivity="internal",
                         authorized_principals=frozenset({"operator"}),
                         source_kind="protected_tool", integrity="trusted",
                         integrity_effect="active_ingest")
    labels = classify_protected_result(
        "pr_rerequest_review", {"repository": "owner/repo", "pull_request": 7}, auth,
        ToolAuthorization(tool_name="pr_rerequest_review", decision="resource_scoped",
                          allowed=True, repo_pr_action_scope=scope),
        result={"ok": False}, provenance=ProtectedResultProvenance((source,)), failed=True,
    )
    # The pre-existing NONE-origin branch forwards native provenance verbatim.
    assert labels.sources == (source,)


@pytest.mark.parametrize("change", ["stdout", "stderr", "git_context", "wrong_head",
    "wrong_scope", "raw_code", "no_summary", "bad_node", "bad_suite", "bad_selector",
    "missing_definition", "parameter_prose", "total_bytes", "inactive_lease", "zero_exit",
    "non_ascii_node", "lease_head_moved", "inventory_not_recorded",
    "inventory_not_recorded_empty_failing"])
def test_failed_repo_test_provenance_requires_exact_bounded_summary(change, tmp_path):
    from copy import deepcopy
    from mimir.access_control import ProtectedResultProvenance

    auth, scope, lease, _, _ = _real_attested_lease(tmp_path)
    test_file = lease.path / "tests" / "test_a.py"
    test_file.parent.mkdir()
    test_file.write_text("def test_a():\n    pass\n")
    _remember_inventory(lease, scope)
    source = SourceLabel(principal="operator", domain="repository",
                         resource_id=f"owner/repo#pull/7@{scope.observed_head_sha}",
                         bridge_instance="forge", sensitivity="internal",
                         authorized_principals=frozenset({"operator"}),
                         source_kind="protected_tool", integrity="trusted",
                         integrity_effect="active_ingest")
    result = {
        "ok": False, "code": "tests_failed", "exit_code": 1, "suite": "default",
        "selectors": [], "summary": {"failed": 1, "errors": None, "passed": 2,
        "skipped": 0, "failing": ["tests/test_a.py::test_a"], "failing_dropped": 0,
        "head": scope.observed_head_sha},
        "remediation_guidance": (
            "The summary lists failing node ids. Prefer reading the lease's test source "
            "and rerunning selected ids. include_output=true reveals raw output, "
            "marks the turn untrusted, and blocks further repo_test runs this turn."
        ),
    }
    authz = ToolAuthorization(tool_name="repo_test", decision="resource_scoped", allowed=True,
                              repo_pr_action_scope=scope)

    def integrity(payload, proof=source):
        labels = classify_protected_result(
            "repo_test", {"repository": "owner/repo", "pull_request": 7}, auth,
            authz, result=payload, provenance=ProtectedResultProvenance((proof,)), failed=True,
        )
        return labels.sources[0].integrity

    assert integrity(result) == "trusted"
    altered = deepcopy(result)
    if change in {"stdout", "stderr", "git_context"}:
        altered[change] = "network-derived text"
    elif change == "wrong_head":
        altered["summary"]["head"] = "b" * 40
    elif change == "raw_code":
        altered["code"] = "test_timeout"
    elif change == "no_summary":
        del altered["summary"]
    elif change == "bad_node":
        altered["summary"]["failing"] = ["tests/test_a.py::test_<injection>"]
    elif change == "bad_suite":
        altered["suite"] = "suite with spaces"
    elif change == "bad_selector":
        altered["selectors"] = ["test with spaces"]
    elif change == "missing_definition":
        altered["summary"]["failing"] = ["tests/test_a.py::test_arbitrary_instruction"]
    elif change == "parameter_prose":
        altered["summary"]["failing"] = ["tests/test_a.py::test_a[merge_now]"]
    elif change == "total_bytes":
        name = "test_" + "x" * 150
        test_file.write_text(f"def {name}(): pass\n")
        _remember_inventory(lease, scope)
        altered["summary"]["failing"] = [f"tests/test_a.py::{name}"] * 50
    elif change == "inactive_lease":
        lease.is_active = False
    elif change == "zero_exit":
        altered["exit_code"] = 0
    elif change == "non_ascii_node":
        # A real definition whose path falls outside the node-id charset is
        # still refused: the inventory alone does not admit arbitrary text.
        (lease.path / "tests" / "test_b c.py").write_text("def test_b():\n    pass\n")
        _remember_inventory(lease, scope)
        assert "tests/test_b c.py::test_b" in _recorded(lease, scope)
        altered["summary"]["failing"] = ["tests/test_b c.py::test_b"]
    elif change == "lease_head_moved":
        lease.head_sha = "c" * 40
    elif change == "inventory_not_recorded":
        from mimir import project_tests
        project_tests._NODE_INVENTORIES.pop(project_tests._inventory_key(lease.path, scope.scope_id), None)
    elif change == "inventory_not_recorded_empty_failing":
        # Empty ``failing`` must not pass vacuously when no run recorded an inventory.
        from mimir import project_tests
        project_tests._NODE_INVENTORIES.pop(project_tests._inventory_key(lease.path, scope.scope_id), None)
        altered["summary"]["failing"] = []
    if change == "wrong_scope":
        source = replace(source, resource_id="owner/repo#pull/7@" + "b" * 40)
    assert integrity(altered, source) == "untrusted"


@pytest.mark.asyncio
async def test_repo_test_scope_resolution_fault_publishes_no_attestation(monkeypatch):
    from langchain_core.tools import ToolException
    from mimir.tools import repo

    def no_state(*_args):
        raise ToolException("scope unavailable")

    monkeypatch.setattr(repo, "_state", no_state)
    token = begin_protected_result_capture()
    try:
        with pytest.raises(ToolException):
            await repo.repo_test.coroutine("owner/repo", 7)
    finally:
        provenance = end_protected_result_capture(token)
    assert provenance is None


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["test_timeout", "test_output_overflow", "tests_failed_output_overflow", "tests_passed"])
async def test_repo_test_non_summary_results_keep_original_shape(tmp_path, monkeypatch, code):
    from dataclasses import asdict
    from mimir.project_tests import ProjectTestResult
    from mimir.tools import repo

    auth, scope, lease, _, _ = _real_attested_lease(tmp_path)
    monkeypatch.setattr(repo, "_state", lambda *_: auth.repo_review_state)
    original = ProjectTestResult(code == "tests_passed", code, 0 if code == "tests_passed" else 1,
                                 stdout="raw stdout", stderr="raw stderr", git_context="git")

    async def execute(self, selectors, *, suite):
        return original

    monkeypatch.setattr(repo.RepoProjectTests, "execute", execute)
    token = begin_protected_result_capture()
    try:
        returned = await repo.repo_test.coroutine("owner/repo", 7, runtime=SimpleNamespace(context=auth))
    finally:
        provenance = end_protected_result_capture(token)
    expected = asdict(original)
    expected.pop("failure_summary")
    assert {key: value for key, value in returned.items() if key != "remediation_guidance"} == expected
    labels = classify_protected_result(
        "repo_test", {"repository": "owner/repo", "pull_request": 7}, auth,
        ToolAuthorization(tool_name="repo_test", decision="resource_scoped", allowed=True,
                          repo_pr_action_scope=scope), result=returned, provenance=provenance,
        failed=code != "tests_passed",
    )
    assert labels.sources[0].integrity == ("trusted" if code == "tests_passed" else "untrusted")


@pytest.mark.parametrize("verdict", [True, False, None])
@pytest.mark.parametrize("operation", [
    "repo_fetch", "repo_status", "repo_diff", "repo_unmerged", "repo_stage",
    "repo_commit", "repo_merge", "repo_merge_abort", "repo_rebase",
    "repo_rebase_abort", "repo_revert", "repo_revert_abort", "repo_push",
])
def test_successful_git_operations_publish_only_attested_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _self_login, verdict, operation,
) -> None:
    from mimir.repo_tools import GitOperationResult
    from mimir.tools import repo

    monkeypatch.setattr(access_control_module, "_lease_head_is_author_attested", _self_login)
    auth, scope, lease, _, _ = _real_attested_lease(tmp_path)
    auth.ifc_state.pr_checkout_author_trust[scope.scope_id] = verdict
    state = auth.repo_review_state
    assert state is not None
    monkeypatch.setattr(repo, "_state", lambda *_: state)

    class FakeGit:
        execution_started = False

        def __init__(self, *_args, **_kwargs):
            pass

        def execute(self, _operation):
            return GitOperationResult(True, "ok")

    monkeypatch.setattr(repo, "RepoGitTools", FakeGit)
    arguments = {
        "repo_stage": {"paths": ("work.py",)},
        "repo_commit": {"paths": ("work.py",), "message": "update"},
        "repo_rebase": {}, "repo_revert": {"commit": scope.observed_head_sha},
    }.get(operation, {})
    tool = getattr(repo, operation)
    token = begin_protected_result_capture()
    try:
        result = tool.func("owner/repo", 7, runtime=SimpleNamespace(context=auth), **arguments)
    finally:
        provenance = end_protected_result_capture(token)
    labels = classify_protected_result(
        operation, {"repository": "owner/repo", "pull_request": 7}, auth,
        ToolAuthorization(tool_name=operation, decision="resource_scoped", allowed=True,
                          repo_pr_action_scope=scope), result=result, provenance=provenance,
    )
    if operation == "repo_stage" and verdict is not True:
        assert labels is None  # stage acknowledgements do not ingest without provenance
        return
    assert len(labels.sources) == 1
    assert labels.sources[0].resource_id == f"owner/repo#pull/7@{scope.observed_head_sha}"
    assert labels.sources[0].integrity == ("trusted" if verdict is True else "untrusted")


@pytest.mark.parametrize("tool_name", ["pr_job_log", "pr_comment"])
def test_non_author_content_repository_results_remain_untrusted(
    tool_name: str,
) -> None:
    auth = _auth()
    scope = auth.repo_pr_action_scope
    assert scope is not None
    labels = classify_protected_result(
        tool_name, {"repository": "owner/repo", "pull_request": 7}, auth,
        ToolAuthorization(
            tool_name=tool_name, decision=OperationDecision.RESOURCE_SCOPED,
            allowed=True, repo_pr_action_scope=scope,
        ),
        result="external output",
    )

    assert labels is not None
    source, = labels.sources
    assert (source.integrity, source.integrity_effect) == (
        "untrusted", "active_ingest",
    )


@pytest.mark.parametrize(
    ("verdict", "mismatch"),
    [(True, None), (False, None), (None, None),
     (True, "number"), (True, "head_sha"), (True, "verified_own_push"),
     (True, "author"), (True, "missing_author"), (True, "no_attestation")],
)
def test_checkout_records_native_author_trust_for_file_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    verdict: bool | None, mismatch: str | None,
) -> None:
    from dataclasses import replace

    from mimir.forge import PullRequestProjection
    from mimir.tools import forge, repo

    author = "" if mismatch == "missing_author" else "collaborator"
    scope = _scope(author=author)
    if mismatch == "verified_own_push":
        import uuid

        scope = replace(scope, observed_head_sha=uuid.uuid4().hex + "0" * 8)
    auth = _auth(scope=scope, recorded_verdict=mismatch is not None)
    runtime = SimpleNamespace(context=auth)
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    checkout, target, _ = _recorded_lease(lease_root, scope=scope)
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(lease_root))
    lease = active_pr_checkout_lease_for_path(target)
    assert lease is not None
    state = RepoReviewState(action_scope=scope)
    auth.server_discovered_pr_states.remember(state)
    monkeypatch.setattr(forge, "remediation_checkout_preflight", lambda *args: (state, None))
    monkeypatch.setattr(repo, "acquire_pr_checkout_lease", lambda *args, **kwargs: (lease, ()))
    metadata = PullRequestProjection(
        7, "Title", "open", author, False, "main", "change",
        "a" * 40, True, "created", "updated",
    )
    if mismatch in {"number", "head_sha", "author"}:
        metadata = replace(metadata, **{
            mismatch: {"number": 8, "head_sha": "c" * 40, "author": "other"}[mismatch],
        })
        auth.ifc_state.repository_author_trust.resolve(
            "owner/repo", "collaborator", lambda: True,
        )
    if mismatch == "verified_own_push":
        from mimir.repo_tools import _record_verified_push, was_verified_push

        _record_verified_push(scope, scope.observed_head_sha, metadata.head_sha)
        assert was_verified_push(
            scope.canonical_repo, scope.pr_number, scope.observed_head_sha, metadata.head_sha,
        )
    calls = []

    def attest(repository, author):
        calls.append((repository, author))
        return verdict

    client = SimpleNamespace(
        get_pull_request=lambda scope: metadata,
        get_diff=lambda scope: "diff --git a/src/work.py b/src/work.py",
        author_is_trusted=attest,
    )
    if mismatch == "no_attestation":
        client.author_is_trusted = None
    monkeypatch.setattr(forge, "_client", lambda scope: client)
    result = repo.repo_checkout.func("owner/repo", 7, runtime=runtime)
    assert result["status"] == "checked_out"
    assert result["path"] == str(checkout)
    assert auth.ifc_state.pr_checkout_author_trust[scope.scope_id] is (
        verdict if mismatch is None else None
    )
    assert calls == ([] if mismatch else [("owner/repo", "collaborator")])

    # The checkout's own tool result carries the same exact-scope provenance
    # as subsequent lease reads, but only after an affirmative native verdict.
    token = begin_protected_result_capture()
    try:
        repo.repo_checkout.func("owner/repo", 7, runtime=runtime)
    finally:
        checkout_provenance = end_protected_result_capture(token)
    checkout_labels = classify_protected_result(
        "repo_checkout", {"repository": "owner/repo", "pull_request": 7}, auth,
        ToolAuthorization(tool_name="repo_checkout", decision="resource_scoped", allowed=True,
                          repo_pr_action_scope=scope),
        result=result, provenance=checkout_provenance,
    )
    assert checkout_labels.sources[0].integrity == (
        "trusted" if verdict is True and mismatch is None else "untrusted"
    )

    def no_attestation(*args):
        pytest.fail("filesystem read attempted author attestation")

    monkeypatch.setattr(client, "author_is_trusted", no_attestation)
    labels = classify_protected_result(
        "read_file", {"file_path": str(target)}, auth,
        ToolAuthorization(tool_name="read_file", decision="resource_scoped", allowed=True),
        result=target.read_text(),
    )
    trusted = verdict is True and mismatch is None
    assert labels is not None
    assert len(labels.sources) == 1
    source = labels.sources[0]
    assert (source.domain, source.integrity, source.integrity_effect) == (
        ("repository", "trusted", "informational") if trusted
        else ("filesystem", "untrusted", "active_ingest")
    )
    assert source.resource_id == (
        f"owner/repo#pull/7@{'a' * 40}" if trusted else str(target)
    )
    auth.ifc_state.merge(labels)
    assert auth.ifc_state.has_untrusted_active_ingest() is (not trusted)
    if mismatch is not None:
        return

    # Definitive verdicts are shared; unavailable attestations must be retried.
    if verdict is None:
        monkeypatch.setattr(client, "author_is_trusted", attest)
    token = begin_protected_result_capture()
    try:
        result = forge.pr_diff.func("owner/repo", 7, runtime=runtime)
    finally:
        provenance = end_protected_result_capture(token)
    assert provenance is not None
    labels = classify_protected_result(
        "pr_diff", {"repository": "owner/repo", "pull_request": 7}, auth,
        ToolAuthorization(
            tool_name="pr_diff", decision="resource_scoped", allowed=True,
            repo_pr_action_scope=scope,
        ),
        result=result, provenance=provenance,
    )
    assert labels is not None
    assert len(labels.sources) == 1
    source = labels.sources[0]
    assert (source.domain, source.integrity, source.integrity_effect) == (
        "repository", "trusted" if trusted else "untrusted", "active_ingest",
    )
    assert source.resource_id == f"owner/repo#pull/7@{'a' * 40}"
    auth.ifc_state.merge(labels)
    assert auth.ifc_state.has_untrusted_active_ingest() is (not trusted)
    assert len(calls) == (3 if verdict is None else 1)


@pytest.mark.parametrize("allowlist,author,trusted", [
    ("", "dependabot[bot]", False),
    ("DePeNdAbOt[bot]", "dependabot[bot]", True),
    ("dependabot[bot]", "DEPENDABOT[BOT]", True),
    ("dependabot[bot]", "renovate[bot]", False),
])
def test_checkout_bot_attestation_requires_exact_operator_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allowlist, author, trusted,
) -> None:
    from dataclasses import replace
    from mimir.forge import PullRequestProjection
    from mimir.tools import forge, repo

    monkeypatch.setenv("MIMIR_GITHUB_TRUSTED_BOT_LOGINS", allowlist)
    scope = _scope(author=author)
    auth = _auth(scope=scope, recorded_verdict=False)
    runtime = SimpleNamespace(context=auth)
    root = tmp_path / "leases"
    root.mkdir()
    _, target, _ = _recorded_lease(root, scope=scope)
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(root))
    lease = active_pr_checkout_lease_for_path(target)
    state = RepoReviewState(scope)
    auth.server_discovered_pr_states.remember(state)
    monkeypatch.setattr(forge, "remediation_checkout_preflight", lambda *_: (state, None))
    monkeypatch.setattr(repo, "acquire_pr_checkout_lease", lambda *args, **kwargs: (lease, ()))
    metadata = replace(PullRequestProjection(
        7, "Title", "open", "author", False, "main", "change",
        "a" * 40, True, "created", "updated",
    ), author=author)
    # No collaborator adapter: the mixed-case bot path must still attest.
    monkeypatch.setattr(forge, "_client", lambda _: SimpleNamespace(
        get_pull_request=lambda _: metadata,
    ))
    token = begin_protected_result_capture()
    try:
        result = repo.repo_checkout.func("owner/repo", 7, runtime=runtime)
    finally:
        provenance = end_protected_result_capture(token)
    labels = classify_protected_result(
        "repo_checkout", {"repository": "owner/repo", "pull_request": 7}, auth,
        ToolAuthorization(tool_name="repo_checkout", decision="resource_scoped", allowed=True,
                          repo_pr_action_scope=scope), result=result, provenance=provenance,
    )
    assert (labels.sources[0].integrity == "trusted") is trusted


def _git_commit(path: Path, target: Path, message: str, author: str, committer: str) -> str:
    target.write_text(message + "\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", target.name], check=True)
    env = {
        "GIT_AUTHOR_NAME": author, "GIT_AUTHOR_EMAIL": f"{author}@example.test",
        "GIT_COMMITTER_NAME": committer, "GIT_COMMITTER_EMAIL": f"{committer}@example.test",
    }
    from mimir.git_bootstrap import DEFAULT_USER_EMAIL, DEFAULT_USER_NAME
    if committer == DEFAULT_USER_NAME:
        env["GIT_COMMITTER_EMAIL"] = DEFAULT_USER_EMAIL
    if author == DEFAULT_USER_NAME:
        env["GIT_AUTHOR_EMAIL"] = DEFAULT_USER_EMAIL
    import os
    subprocess.run(["git", "-C", str(path), "commit", "-qm", message], check=True,
                   env={**os.environ, **env})
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.mark.parametrize("operation", ["merge", "rebase"])
def test_protected_base_lineage_keeps_results_and_file_reads_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _self_login, operation: str,
) -> None:
    from dataclasses import replace
    from mimir.git_bootstrap import DEFAULT_USER_NAME
    from mimir.tools.repo import _publish_attested_lease_result

    monkeypatch.setattr(access_control_module, "_lease_head_is_author_attested", _self_login)
    auth, original, lease, target, _ = _real_attested_lease(tmp_path)
    subprocess.run(["git", "-C", str(lease.path), "branch", "-m", "main"], check=True)
    # A real fork: the attested range contains a human display name, not
    # the verified forge login. Replay must rely on the original patch only.
    common = original.observed_head_sha
    attested = _git_commit(lease.path, target, "feature", "Jason Carreira", "Jason Carreira")
    original = replace(original, observed_head_sha=attested)
    object.__setattr__(lease, "head_sha", attested)
    subprocess.run(["git", "-C", str(lease.path), "checkout", "-qb", "base", common], check=True)
    base_file = lease.path / "base.txt"
    base = _git_commit(lease.path, base_file, "base", "outsider", "outsider")
    subprocess.run(["git", "-C", str(lease.path), "checkout", "-q", "main"], check=True)
    if operation == "merge":
        subprocess.run(["git", "-C", str(lease.path), "-c", f"user.name={DEFAULT_USER_NAME}",
                        "-c", "user.email=noreply@mimir-agent.local", "merge", "-q",
                        "--allow-unrelated-histories", "--no-ff", "base", "-m", "merge base"], check=True)
    else:
        # Preserve the human display name while the server is the committer.
        subprocess.run(["git", "-C", str(lease.path),
                        "-c", f"user.name={DEFAULT_USER_NAME}",
                        "-c", "user.email=noreply@mimir-agent.local", "rebase", "-q",
                        "--onto", "base", common, "main"], check=True)
        auth.ifc_state.repository_author_trust.resolve("owner/repo", "jasoncarreira", lambda: True)
    scope = replace(original, destination_ref="refs/heads/main", observed_base_sha=base)
    object.__setattr__(lease, "scope_id", scope.scope_id)
    object.__setattr__(lease, "base_sha", base)
    object.__setattr__(auth, "repo_pr_action_scope", scope)
    auth.ifc_state.pr_checkout_author_trust[scope.scope_id] = True
    state = RepoReviewState(scope)
    state.attach_checkout_lease(lease)
    head = subprocess.run(["git", "-C", str(lease.path), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()
    state.record_git_head(scope.scope_id, head)
    object.__setattr__(auth, "repo_review_state", state)
    assert access_control_module._attested_pr_checkout_lease(auth, scope, lease)
    from mimir import pr_checkout_lease

    monkeypatch.setattr(pr_checkout_lease, "active_pr_checkout_lease_for_path", lambda _: lease)
    read_source = protected_result_source(
        auth, principal="filesystem", domain="filesystem", resource_id=str(target),
        bridge_instance="filesystem",
    )
    assert (read_source.domain, read_source.integrity) == ("repository", "trusted")
    wrong_base = SimpleNamespace(**{**vars(lease), "base_sha": "f" * 40})
    if operation == "rebase":
        assert not access_control_module._lease_head_is_author_attested(
            lease.path, "main", original.observed_head_sha, head,
            scope=scope, lease=wrong_base, ifc_state=auth.ifc_state,
        )
    assert not access_control_module._lease_head_is_author_attested(
        lease.path, "other-branch", original.observed_head_sha, head,
        scope=scope, lease=lease, ifc_state=auth.ifc_state,
    )
    unprotected = replace(scope, destination_ref="refs/heads/main-copy")
    assert not access_control_module._lease_head_is_author_attested(
        lease.path, "main", original.observed_head_sha, head,
        scope=unprotected, lease=lease, ifc_state=auth.ifc_state,
    )
    if operation == "rebase":
        # A conflict-resolution edit is not a patch-identical replay, even with
        # the original human author and the server committer.
        target.write_text("altered replay\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(lease.path), "add", "work.py"], check=True)
        subprocess.run(["git", "-C", str(lease.path), "-c", f"user.name={DEFAULT_USER_NAME}",
                        "-c", "user.email=noreply@mimir-agent.local", "commit", "-q",
                        "--amend", "--no-edit"], check=True)
        altered = subprocess.run(["git", "-C", str(lease.path), "rev-parse", "HEAD"], check=True,
                                 capture_output=True, text=True).stdout.strip()
        assert not access_control_module._lease_head_is_author_attested(
            lease.path, "main", original.observed_head_sha, altered,
            scope=scope, lease=lease, ifc_state=auth.ifc_state,
        )
        subprocess.run(["git", "-C", str(lease.path), "reset", "-q", "--hard", head], check=True)
    auth.ifc_state.repository_author_trust.resolve("owner/repo", "jasoncarreira", lambda: True)
    for author, committer in (
        ("outsider", "outsider"), ("collaborator", "outsider"),
        ("uncached", DEFAULT_USER_NAME), ("jasoncarreira", DEFAULT_USER_NAME),
    ):
        bad_head = _git_commit(lease.path, target, author + committer, author, committer)
        assert not access_control_module._lease_head_is_author_attested(
            lease.path, "main", original.observed_head_sha, bad_head,
            scope=scope, lease=lease, ifc_state=auth.ifc_state,
        )
        subprocess.run(["git", "-C", str(lease.path), "reset", "-q", "--hard", head], check=True)
    # An unrelated HEAD is neither a descendant of the PR head nor the base.
    subprocess.run(["git", "-C", str(lease.path), "checkout", "-q", "--orphan", "unrelated"], check=True)
    subprocess.run(["git", "-C", str(lease.path), "rm", "-qrf", "."], check=True)
    detached = _git_commit(lease.path, target, "unrelated", DEFAULT_USER_NAME, DEFAULT_USER_NAME)
    subprocess.run(["git", "-C", str(lease.path), "branch", "-f", "main", detached], check=True)
    subprocess.run(["git", "-C", str(lease.path), "checkout", "-q", "main"], check=True)
    assert not access_control_module._lease_head_is_author_attested(
        lease.path, "main", original.observed_head_sha, detached,
        scope=scope, lease=lease, ifc_state=auth.ifc_state,
    )
    subprocess.run(["git", "-C", str(lease.path), "reset", "-q", "--hard", head], check=True)
    for tool in ("repo_status", "repo_test"):
        capture = begin_protected_result_capture()
        try:
            _publish_attested_lease_result(SimpleNamespace(context=auth), state)
        finally:
            provenance = end_protected_result_capture(capture)
        labels = classify_protected_result(tool, {"repository": "owner/repo", "pull_request": 7},
            auth, ToolAuthorization(tool_name=tool, decision="resource_scoped", allowed=True,
                                    repo_pr_action_scope=scope), result="ok", provenance=provenance)
        assert labels.sources[0].integrity == "trusted"


def _replay_test_repo(tmp_path: Path):
    """A real attested patch with a whitespace-sensitive statement addition."""
    from dataclasses import replace
    from mimir.git_bootstrap import DEFAULT_USER_EMAIL, DEFAULT_USER_NAME

    _, original, lease, target, _ = _real_attested_lease(tmp_path)
    path = lease.path
    subprocess.run(["git", "-C", str(path), "branch", "-m", "main"], check=True)

    def git(*args):
        return subprocess.run([
            "git", "-C", str(path), "-c", f"user.name={DEFAULT_USER_NAME}",
            "-c", f"user.email={DEFAULT_USER_EMAIL}", *args,
        ], check=True, capture_output=True, text=True).stdout.strip()

    def commit(content, author="Jason Carreira"):
        target.write_text(content, encoding="utf-8")
        git("add", "work.py")
        git("commit", "-qm", "statement", "--author", f"{author} <{author.replace(' ', '')}@example.test>")
        return git("rev-parse", "HEAD")

    common_text = "if enabled:\n    preserve_everything()\n"
    patch_text = common_text + "    delete_everything()\n"
    common = commit(common_text)
    attested = commit(patch_text)
    git("checkout", "-qb", "base", common)
    (path / "base.txt").write_text("protected base\n", encoding="utf-8")
    git("add", "base.txt")
    git("commit", "-qm", "protected base")
    base = git("rev-parse", "HEAD")
    git("checkout", "-q", "main")
    scope = replace(original, observed_head_sha=attested,
                    observed_base_sha=base, destination_ref="refs/heads/main")
    lease.base_sha = base
    return path, target, common, attested, scope, lease, git, commit, common_text, patch_text


def test_replay_indentation_semantic_change_is_untrusted(tmp_path, _self_login):
    path, target, common, attested, scope, lease, git, _, _, patch_text = _replay_test_repo(tmp_path)
    git("rebase", "-q", "--onto", "base", common, "main")
    replay = git("rev-parse", "HEAD")
    assert _self_login(path, "main", attested, replay, scope=scope, lease=lease)
    # Same tokens, but the destructive call is now unconditional. Stable
    # patch-id accepts this; verbatim must reject it.
    altered_text = patch_text.replace("    delete_everything()", "delete_everything()")
    target.write_text(altered_text, encoding="utf-8")
    git("add", "work.py")
    git("commit", "-q", "--amend", "--no-edit")
    assert not _self_login(path, "main", attested, git("rev-parse", "HEAD"),
                           scope=scope, lease=lease)


@pytest.mark.parametrize("original_copies, trusted", [(1, False), (2, True)])
def test_replayed_patch_consumes_one_original_match(tmp_path, _self_login, original_copies, trusted):
    from dataclasses import replace
    from mimir.git_bootstrap import DEFAULT_USER_EMAIL, DEFAULT_USER_NAME

    path, _, _, attested, scope, lease, git, commit, common_text, patch_text = _replay_test_repo(tmp_path)
    if original_copies == 2:
        # Two genuine occurrences in the attested range must remain usable.
        commit(common_text, DEFAULT_USER_NAME)
        attested = commit(patch_text)
        scope = replace(scope, observed_head_sha=attested)
    git("reset", "-q", "--hard", "base")
    git("cherry-pick", attested)
    first_replay = git("rev-parse", "HEAD")
    assert _self_login(path, "main", attested, first_replay, scope=scope, lease=lease)
    git("revert", "--no-commit", first_replay)
    # This intermediary is server-authored, not another attested replay.
    git("commit", "-qm", "server revert", "--author", f"{DEFAULT_USER_NAME} <{DEFAULT_USER_EMAIL}>")
    git("cherry-pick", attested)
    assert _self_login(path, "main", attested, git("rev-parse", "HEAD"),
                       scope=scope, lease=lease) is trusted


@pytest.mark.parametrize("count, trusted", [(500, True), (501, False)])
def test_original_patch_range_budget_fails_before_patch_subprocesses(monkeypatch, _self_login, count, trusted):
    from mimir import repo_tools
    from mimir.git_bootstrap import DEFAULT_USER_EMAIL, DEFAULT_USER_NAME

    head, base, replay, original = (c * 40 for c in "abcd")
    calls = []

    def run(_path, arguments, **_kwargs):
        calls.append(arguments)
        if arguments[0] == "log":
            output = "\x00".join([replay, base, "Jason Carreira", "human@example.test",
                                   DEFAULT_USER_NAME, DEFAULT_USER_EMAIL, ""])
        elif arguments[0] == "merge-base":
            output = original + "\n"
        elif arguments[0] == "rev-list":
            assert "--max-count=501" in arguments
            output = (head + "\n") * count
        elif arguments[0] == "show":
            output = "immutable diff"
        else:
            pytest.fail(f"unexpected Git command: {arguments}")
        return SimpleNamespace(returncode=0, stdout=output, timed_out=False, output_limited=False)

    patch_calls = []

    def patch_run(argv, **kwargs):
        assert argv[-2:] == ["patch-id", "--verbatim"]
        patch_calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=f"{'f' * 40} {'0' * 40}\n")

    monkeypatch.setattr(repo_tools, "hardened_git_command", run)
    monkeypatch.setattr(access_control_module.subprocess, "run", patch_run)
    scope = SimpleNamespace(observed_base_sha=base, destination_ref="refs/heads/main")
    lease = SimpleNamespace(base_sha=base)
    assert _self_login(Path("/unused"), "main", head, replay, observed_state=("main", replay),
                       scope=scope, lease=lease) is trusted
    assert len(patch_calls) == (501 if trusted else 0)
    assert sum(args[0] == "show" for args in calls) == (501 if trusted else 0)


@pytest.mark.parametrize("author", ["collaborator", "mimir-bot"])
def test_lease_without_recorded_verdict_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, author: str,
) -> None:
    scope = _scope(author=author)
    auth = _auth(scope=scope, recorded_verdict=False)
    auth.ifc_state.repository_author_trust.resolve("owner/repo", author, lambda: True)
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    _, target, _ = _recorded_lease(lease_root, scope=scope)
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(lease_root))

    source = protected_result_source(
        auth, principal="filesystem", domain="filesystem",
        resource_id=str(target), bridge_instance="filesystem",
    )

    assert (source.domain, source.integrity, source.integrity_effect) == (
        "filesystem", "untrusted", "active_ingest",
    )
    auth.ifc_state.merge(InformationFlowLabels().with_source(source))
    assert auth.ifc_state.has_untrusted_active_ingest()



def test_metadata_failure_after_acquisition_clears_recorded_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transport failure on re-acquisition must not leave a stale trusted verdict.

    The verdict is cleared before metadata is fetched precisely so a raising
    fetch fails closed. Forge ConnectionErrors are observed in production, so
    this is a reachable path, not a theoretical one.
    """
    from mimir.forge import PullRequestProjection
    from mimir.tools import forge, repo

    scope = _scope(author="collaborator")
    auth = _auth(scope=scope)
    runtime = SimpleNamespace(context=auth)
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    checkout, target, _ = _recorded_lease(lease_root, scope=scope)
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(lease_root))
    lease = active_pr_checkout_lease_for_path(target)
    assert lease is not None
    state = RepoReviewState(action_scope=scope)
    auth.server_discovered_pr_states.remember(state)
    monkeypatch.setattr(forge, "remediation_checkout_preflight", lambda *args: (state, None))
    monkeypatch.setattr(repo, "acquire_pr_checkout_lease", lambda *args, **kwargs: (lease, ()))
    metadata = PullRequestProjection(
        7, "Title", "open", "collaborator", False, "main", "change",
        "a" * 40, True, "created", "updated",
    )
    client = SimpleNamespace(
        get_pull_request=lambda scope: metadata,
        get_diff=lambda scope: "diff --git a/src/work.py b/src/work.py",
        author_is_trusted=lambda repository, author: True,
    )
    monkeypatch.setattr(forge, "_client", lambda scope: client)

    # First acquisition records an affirmative verdict.
    assert repo.repo_checkout.func("owner/repo", 7, runtime=runtime)["path"] == str(checkout)
    assert auth.ifc_state.pr_checkout_author_trust[scope.scope_id] is True

    # Re-acquisition where the metadata fetch raises, as a forge transport
    # failure does. The recorded verdict must not survive it.
    def failing_metadata(scope):
        raise ConnectionError("forge transport failed")

    monkeypatch.setattr(client, "get_pull_request", failing_metadata)
    with pytest.raises(Exception):
        repo.repo_checkout.func("owner/repo", 7, runtime=runtime)
    assert auth.ifc_state.pr_checkout_author_trust[scope.scope_id] is None

    # And the lease read that the stale verdict would have trusted is untrusted.
    labels = classify_protected_result(
        "read_file", {"file_path": str(target)}, auth,
        ToolAuthorization(tool_name="read_file", decision="resource_scoped", allowed=True),
        result=target.read_text(),
    )
    assert labels is not None
    source = labels.sources[0]
    assert (source.domain, source.integrity, source.integrity_effect) == (
        "filesystem", "untrusted", "active_ingest",
    )

@pytest.mark.parametrize("tool_name", ["read_file", "grep"])
def test_active_lease_file_results_use_repository_source_labels(
    tool_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    _checkout, target, _record = _recorded_lease(lease_root)
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(lease_root))
    auth = _auth()

    labels = classify_protected_result(
        tool_name,
        {"path": str(target), "file_path": str(target)},
        auth,
        ToolAuthorization(
            tool_name=tool_name,
            decision=OperationDecision.RESOURCE_SCOPED,
            allowed=True,
        ),
        result="ok",
    )

    assert labels is not None
    assert len(labels.sources) == 1
    source = labels.sources[0]
    assert source.principal == "operator"
    assert source.domain == "repository"
    assert source.resource_id == f"owner/repo#pull/7@{'a' * 40}"
    assert source.bridge_instance == "forge"
    assert source.integrity == "trusted"
    assert source.integrity_effect == "informational"


def test_cross_pr_scope_keeps_lease_read_untrusted_filesystem_ingest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    _checkout, target, _record = _recorded_lease(lease_root)
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(lease_root))
    monkeypatch.setenv("MIMIR_GITHUB_SELF_LOGIN", "mimir-bot")

    source = protected_result_source(
        _auth(pr_number=8), principal="filesystem", domain="filesystem",
        resource_id=str(target), bridge_instance="filesystem",
    )

    assert source.domain == "filesystem"
    assert (source.integrity, source.integrity_effect) == (
        "untrusted", "active_ingest",
    )


def _assert_scope_mismatch_is_untrusted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    field: str,
    value: object,
) -> None:
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    _checkout, target, _record = _recorded_lease(lease_root)
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(lease_root))
    scope = _scope()
    object.__setattr__(scope, field, value)

    source = protected_result_source(
        _auth(scope=scope), principal="filesystem", domain="filesystem",
        resource_id=str(target), bridge_instance="filesystem",
    )

    assert source.domain == "filesystem"
    assert (source.integrity, source.integrity_effect) == (
        "untrusted", "active_ingest",
    )


def test_different_scope_id_keeps_lease_read_untrusted_filesystem_ingest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _assert_scope_mismatch_is_untrusted(
        tmp_path, monkeypatch, field="scope_id", value="c" * 64,
    )


def test_different_repository_keeps_lease_read_untrusted_filesystem_ingest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _assert_scope_mismatch_is_untrusted(
        tmp_path, monkeypatch, field="canonical_repo", value="other/repo",
    )


def test_different_pr_number_keeps_lease_read_untrusted_filesystem_ingest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _assert_scope_mismatch_is_untrusted(
        tmp_path, monkeypatch, field="pr_number", value=8,
    )


def test_different_head_keeps_lease_read_untrusted_filesystem_ingest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _assert_scope_mismatch_is_untrusted(
        tmp_path, monkeypatch, field="observed_head_sha", value="c" * 40,
    )


def test_edit_file_acknowledgement_does_not_ingest_a_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    _checkout, target, _record = _recorded_lease(lease_root)
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(lease_root))

    labels = classify_protected_result(
        "edit_file",
        {"file_path": str(target)},
        _auth(),
        ToolAuthorization(
            tool_name="edit_file",
            decision=OperationDecision.RESOURCE_SCOPED,
            allowed=True,
        ),
        result="Successfully replaced 1 instance(s)",
    )

    assert labels is None


@pytest.mark.parametrize("record_state", ["missing", "expired", "malformed"])
def test_non_active_lease_record_keeps_filesystem_active_ingest(
    record_state: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    _checkout, target, _record = _recorded_lease(
        lease_root,
        expires_at=(
            datetime.now(UTC) - timedelta(seconds=1)
            if record_state == "expired"
            else None
        ),
    )
    metadata = target.parents[1] / ".git" / "mimir-pr-checkout-lease.json"
    if record_state == "missing":
        metadata.unlink()
    elif record_state == "malformed":
        metadata.write_text("{not-json", encoding="utf-8")
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(lease_root))

    source = protected_result_source(
        _auth(), principal="filesystem", domain="filesystem",
        resource_id=str(target), bridge_instance="filesystem",
    )

    assert source.principal == "filesystem"
    assert source.domain == "filesystem"
    assert source.integrity == "untrusted"
    assert source.integrity_effect == "active_ingest"


def test_lease_path_symlink_escape_is_untrusted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    checkout, _target, _record = _recorded_lease(lease_root)
    outside = tmp_path / "outside.py"
    outside.write_text("outside\n", encoding="utf-8")
    escaped = checkout / "escaped.py"
    escaped.symlink_to(outside)
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(lease_root))

    assert active_pr_checkout_lease_for_path(escaped) is None
    source = protected_result_source(
        _auth(), principal="filesystem", domain="filesystem",
        resource_id=str(escaped), bridge_instance="filesystem",
    )

    assert source.domain == "filesystem"
    assert (source.integrity, source.integrity_effect) == (
        "untrusted", "active_ingest",
    )


def test_lease_repository_sources_only_flow_to_their_own_forge_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    _checkout, target, _record = _recorded_lease(lease_root)
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(lease_root))
    source = protected_result_source(
        _auth(), principal="filesystem", domain="filesystem",
        resource_id=str(target), bridge_instance="filesystem",
    )
    labels = InformationFlowLabels().with_source(source)

    own = _auth(labels)
    own_decision = SinkGate.check_sink_flow(
        "repo_push", "owner/repo", labels, own, enforce=True,
        repo_pr_action_scope=own.repo_pr_action_scope,
    )
    assert own_decision.allowed is True, own_decision.reason

    untrusted = SourceLabel(
        principal="filesystem", domain="filesystem",
        resource_id="/outside/input.txt", bridge_instance="filesystem",
        sensitivity="internal", authorized_principals=frozenset({"operator"}),
        source_kind="protected_tool", integrity="untrusted",
        integrity_effect="active_ingest",
    )
    mixed = labels.with_source(untrusted)
    mixed_auth = _auth(mixed)
    mixed_decision = SinkGate.check_sink_flow(
        "repo_push", "owner/repo", mixed, mixed_auth, enforce=True,
        repo_pr_action_scope=mixed_auth.repo_pr_action_scope,
    )
    assert mixed_decision.allowed is False
    assert mixed_decision.reason == "ifc_label_blocked:forge"

    other = _auth(labels, repository="other/repo")
    other_decision = SinkGate.check_sink_flow(
        "repo_push", "other/repo", labels, other, enforce=True,
        repo_pr_action_scope=other.repo_pr_action_scope,
    )
    assert other_decision.allowed is False
    assert other_decision.reason == "ifc_label_blocked:forge"

    for tool_name, target in (
        ("post_message", "channel-2"),
        ("memory_store", "semantic"),
    ):
        decision = SinkGate.check_sink_flow(
            tool_name, target, labels, own, enforce=True,
        )
        assert decision.allowed is False
