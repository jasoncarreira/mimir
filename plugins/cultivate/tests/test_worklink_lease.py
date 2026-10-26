"""Compatibility and security checks at the Worklink lease extraction boundary."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from contextlib import contextmanager
import errno
import hashlib
import json
import os
import subprocess
from pathlib import Path
import asyncio
import stat
from types import SimpleNamespace

import pytest

from mimir_cultivate.worklink import checkout, claims, continuation, merge_closure, poller_hooks
from mimir_cultivate.worklink.retained_scope import derive_retained_factory_scope
from mimir_cultivate.worklink import retained_scope
from mimir_cultivate.policy import RetainedFactoryScope
from mimir_cultivate.worklink.dispatch_failures import dispatch_failure_state_dir


def test_checkout_capability_requires_issued_directory(tmp_path: Path) -> None:
    issued = tmp_path / "checkout"
    issued.mkdir()
    unissued_fd = os.open(issued, os.O_RDONLY)
    try:
        with pytest.raises(TypeError, match="issued by the checkout factory"):
            checkout.CheckoutAuthorization(issued, 1949, 1, unissued_fd)
    finally:
        os.close(unissued_fd)

    fd = os.open(issued, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    with checkout._mint_checkout_authorization(issued, 1949, 1, fd) as authorization:
        authorization.verify(issued)
        issued.rename(tmp_path / "old")
        issued.symlink_to(tmp_path / "old", target_is_directory=True)
        with pytest.raises(ValueError, match="does not match"):
            authorization.verify(issued)


def test_checkout_open_refuses_intermediate_symlink(tmp_path: Path) -> None:
    root = tmp_path / "root"
    issued = root / "repo" / "1949-1"
    issued.mkdir(parents=True)
    fd = checkout._open_issued_checkout(root, Path("repo/1949-1"))
    try:
        assert os.fstat(fd).st_ino == issued.stat().st_ino
    finally:
        os.close(fd)
    (root / "alias").symlink_to(root / "repo", target_is_directory=True)
    with pytest.raises((OSError, RuntimeError)):
        checkout._open_issued_checkout(root, Path("alias/1949-1"))


def test_selected_checkout_path_and_pruning_ignore_legacy_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    root = tmp_path / "worker-checkouts"
    monkeypatch.setenv("WORKLINK_CHECKOUT_ROOT", str(tmp_path / ".worklink"))
    monkeypatch.setattr(checkout, "_ENABLED_CHECKOUT_ROOT", root)
    monkeypatch.setenv("MIMIR_CODING_ENABLED", "true")
    monkeypatch.setenv("MIMIR_PLUGINS", "")
    assert checkout._isolated_checkout_path(repo, ".worklink", 1949, 1) == (
        tmp_path / ".worklink" / "repo" / "1949-1"
    )
    monkeypatch.delenv("MIMIR_CODING_ENABLED")
    monkeypatch.setenv("MIMIR_PLUGINS", "cultivate")
    path = checkout._isolated_checkout_path(repo, ".worklink", 1949, 1)
    assert path.parent.parent.parent == root
    assert len(path.parent.parent.name) == 64
    assert path.name == "checkout"
    path.parent.mkdir(parents=True)
    os.utime(path.parent, (0, 0))
    removed = checkout.prune_attempt_checkouts(
        repo, older_than=timedelta(days=1), now=datetime.now(UTC),
        runner=lambda args: subprocess.CompletedProcess(args, 0, "", ""),
    )
    assert removed == [path.parent]
    assert not path.parent.exists()


def test_selected_worker_leaf_checkout_is_path_addressed_like_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Leaf builds keep base's model: a worker-traversable path checkout.

    The worklink uid still runs the build (through the executor's
    path-addressed RPC), but the checkout is not FD-only: coding CLIs and git
    must be able to stat their own tree, and siblings share one parent.
    """
    monkeypatch.delenv("MIMIR_CODING_ENABLED", raising=False)
    monkeypatch.setenv("MIMIR_PLUGINS", "cultivate")
    monkeypatch.setenv("WORKLINK_CHECKOUT_ROOT", str(tmp_path / ".worklink"))
    root = tmp_path / "worker-checkouts"
    monkeypatch.setattr(checkout, "_ENABLED_CHECKOUT_ROOT", root)
    monkeypatch.setattr(checkout, "get_identities", lambda: SimpleNamespace(
        mimir_uid=os.getuid(), worklink_gid=os.getgid(),
    ))
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr
        return result

    git("init", "-b", "main", str(repo))
    git("-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.org",
        "commit", "--allow-empty", "-m", "initial")
    git("-C", str(repo), "remote", "add", "origin", str(repo))
    calls: list[list[str]] = []

    def runner(args):
        calls.append(list(args))
        return subprocess.run(args, capture_output=True, text=True, check=False)

    lease = checkout.create_isolated_checkout(
        repo, issue_id=1949, attempt=1, worker_eligible=True, runner=runner,
    )
    sibling = checkout.create_isolated_checkout(
        repo, issue_id=1950, attempt=1, worker_eligible=True, runner=runner,
    )
    assert lease.path == tmp_path / ".worklink" / "repo" / "1949-1"
    assert sibling.path.parent == lease.path.parent
    assert not lease.worker_authorized and lease.authorization is None
    assert not root.exists(), "leaf builds must not use the FD-only issued-checkout root"
    observed = lease.path.stat()
    assert stat.S_IMODE(observed.st_mode) == 0o2770
    assert observed.st_gid == os.getgid()
    assert "--no-hardlinks" in next(call for call in calls if call[:3] == ["git", "clone", "--local"])

    assert checkout.cleanup_checkout(lease, outcome="completed", runner=runner)
    assert checkout.cleanup_checkout(sibling, outcome="completed", runner=runner)
    assert not lease.path.exists() and not sibling.path.exists()


def test_selected_worker_checkout_refuses_replaced_checkout_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MIMIR_CODING_ENABLED", raising=False)
    monkeypatch.setenv("MIMIR_PLUGINS", "cultivate")
    monkeypatch.setenv("WORKLINK_CHECKOUT_ROOT", str(tmp_path / ".worklink"))
    root = tmp_path / "worker-checkouts"
    monkeypatch.setattr(checkout, "_ENABLED_CHECKOUT_ROOT", root)
    monkeypatch.setattr(checkout, "get_identities", lambda: SimpleNamespace(
        mimir_uid=os.getuid(), worklink_gid=os.getgid(),
    ))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test",
                    "-c", "user.email=test@example.org", "commit", "--allow-empty", "-m", "initial"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(repo)],
                   check=True, capture_output=True)
    original = checkout._assert_self_contained_checkout
    external = tmp_path / "external"
    external.mkdir(mode=0o755)
    external.chmod(0o755)
    before = external.stat()
    replaced: list[Path] = []

    def replace(path, *, runner):
        original(path, runner=runner)
        path.rename(path.parent / "old-checkout")
        path.symlink_to(external, target_is_directory=True)
        replaced.append(path)

    monkeypatch.setattr(checkout, "_assert_self_contained_checkout", replace)
    # The worker-accessible leaf is opened by path with O_NOFOLLOW|O_DIRECTORY
    # before its descriptor is normalized, on every platform. A symlinked leaf
    # fails that open itself (ENOTDIR on Linux and macOS; ELOOP is the other
    # POSIX spelling), so normalization never reaches the symlink's target.
    with pytest.raises(OSError) as refused:
        checkout.create_isolated_checkout(
            repo, issue_id=1949, attempt=2, worker_eligible=True,
        )
    assert replaced, "the checkout was never replaced; the test cannot see the bug"
    assert refused.value.errno in {errno.ELOOP, errno.ENOTDIR}
    assert refused.value.filename == str(replaced[0])
    assert replaced[0].is_symlink()
    after = external.stat()
    assert not any(external.iterdir())
    assert (stat.S_IMODE(after.st_mode), after.st_uid, after.st_gid) == (
        stat.S_IMODE(before.st_mode), before.st_uid, before.st_gid,
    )


def test_claim_reset_preserves_wire_format_and_bounded_budget() -> None:
    record = claims.ClaimRecord(1949, 1, "worker", datetime(2026, 10, 1, tzinfo=UTC))
    assert record.to_comment() == "WORKLINK_CLAIM " + json.dumps({
        "issue_id": 1949, "attempt": 1, "agent_id": "worker",
        "claimed_at": "2026-10-01T00:00:00+00:00", "heartbeat_at": None,
    }, sort_keys=True)
    comments = [record.to_comment(), claims.CLAIM_RESET_PREFIX + '{"reason":"repair"}']
    client = claims.ChainlinkClaims(agent_id="worker")
    assert client.next_attempt(comments) == 2
    assert client.attempts_used(comments) == 0
    assert claims.claim_records_from_comments(comments) == [record]


def test_claim_contention_retries_but_real_denial_does_not(tmp_path: Path) -> None:
    attempts = 0
    sleeps: list[float] = []

    def contested(args: list[str]) -> subprocess.CompletedProcess[str]:
        nonlocal attempts
        if args[1:3] == ["locks", "claim"]:
            attempts += 1
            if attempts == 1:
                return subprocess.CompletedProcess(
                    args, 128, "", "fatal: Unable to create '/home/.git/worktrees/locks/index.lock': File exists."
                )
        return subprocess.CompletedProcess(args, 0, "", "")

    result = claims.ChainlinkClaims(
        agent_id="worker", runner=contested, home_path=tmp_path, sleeper=sleeps.append,
    ).claim_issue(1949, labels=["worklink:ready"])
    assert result.claimed and result.record is not None
    assert result.record.attempt == 1
    assert attempts == 2
    assert sleeps == [0.1]

    def denied(args: list[str]) -> subprocess.CompletedProcess[str]:
        if args[1:3] == ["locks", "claim"]:
            return subprocess.CompletedProcess(args, 1, "", "issue locked by other worker")
        return subprocess.CompletedProcess(args, 0, "", "")

    sleeps.clear()
    result = claims.ChainlinkClaims(
        agent_id="worker", runner=denied, home_path=tmp_path, sleeper=sleeps.append,
    ).claim_issue(1949, labels=["worklink:ready"])
    assert not result.claimed
    assert result.reason == "issue locked by other worker"
    assert sleeps == []


def test_shutdown_abort_forgives_claim_without_reusing_ordinal() -> None:
    claimed_at = datetime(2026, 10, 1, tzinfo=UTC)
    record = claims.ClaimRecord(1949, 1, "worker", claimed_at)
    aborted = claims.ShutdownAbortRecord(1949, 1, "worker", claimed_at, claimed_at)
    client = claims.ChainlinkClaims(agent_id="worker")
    comments = [record.to_comment(), aborted.to_comment()]
    assert client.attempts_used(comments) == 0
    assert client.next_attempt(comments) == 2


def test_provider_shutdown_releases_only_owned_claim_and_forgives_budget() -> None:
    now = datetime(2026, 10, 1, tzinfo=UTC)
    own = claims.ClaimRecord(1949, 1, "worker-a", now)
    foreign = claims.ClaimRecord(1950, 1, "worker-b", now)
    calls: list[list[str]] = []

    def runner(args):
        call = list(args)
        calls.append(call)
        if call[1:3] == ["issue", "list"]:
            value = [{"id": 1949}, {"id": 1950}]
        elif call[1:3] == ["issue", "show"]:
            record = own if call[3] == "1949" else foreign
            value = {"comments": [{"content": record.to_comment()}]}
        else:
            value = ""
        return subprocess.CompletedProcess(call, 0, json.dumps(value), "")

    client = claims.ChainlinkClaims(agent_id="worker-a", runner=runner,
                                    clock=lambda: now + timedelta(minutes=1))
    released, failed = client.release_owned_claims_for_shutdown()
    assert released == [own] and failed == []
    assert [call[1:3] for call in calls if "1950" in call] == [["issue", "show"]]
    assert [call[1:3] for call in calls if call[1:3] == ["locks", "release"]] == [
        ["locks", "release"]]
    assert [call[1:3] for call in calls if "1949" in call] == [
        ["issue", "show"], ["issue", "comment"], ["issue", "label"],
        ["locks", "release"], ["issue", "unlabel"],
    ]
    abort = next(call[-1] for call in calls if call[1:3] == ["issue", "comment"])
    assert abort.startswith(claims.SHUTDOWN_ABORT_PREFIX)
    assert client.attempts_used([own.to_comment(), abort]) == 0
    assert client.next_attempt([own.to_comment(), abort]) == 2


def test_continuation_delivery_key_keeps_existing_identity(tmp_path: Path) -> None:
    action = continuation.WorklinkContinuationAction(tmp_path / "key.json", "key", 1949, None, 1)
    assert action.delivery_key == "worklink-continuation:key"
    assert continuation.continuation_sidecar_path(tmp_path, "key") == (
        tmp_path / "state" / "worklink" / "continuations" / "key.json"
    )


def test_continuation_offer_is_stable_until_delivery_receipt(tmp_path: Path) -> None:
    path = continuation.continuation_sidecar_path(tmp_path, "key")
    path.parent.mkdir(parents=True)
    payload = {
        "kind": continuation.CONTINUATION_KIND, "idempotency_key": "key",
        "association": {"issue_id": 1949}, "occurrences": 2,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    first = continuation.consume_worklink_budget_continuations(tmp_path)
    assert len(first) == 1
    assert first[0].delivery_key == "worklink-continuation:key"
    assert first[0].issue_id == 1949
    assert first[0].occurrences == 2
    assert continuation.consume_worklink_budget_continuations(tmp_path) == first
    assert json.loads(path.read_text(encoding="utf-8")) == payload


def test_provider_continuation_delivers_after_receipt_only_once(tmp_path: Path) -> None:
    path = continuation.continuation_sidecar_path(tmp_path, "key")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "kind": continuation.CONTINUATION_KIND, "idempotency_key": "key",
        "association": {"issue_id": 1949}, "occurrences": 1,
    }), encoding="utf-8")
    comments: list[str] = []

    def runner(args, cwd=None):
        assert cwd == tmp_path
        if args[1:3] == ["issue", "show"]:
            value = {"status": "open", "comments": [{"content": comment} for comment in comments]}
            return subprocess.CompletedProcess(args, 0, json.dumps(value), "")
        assert args[1:3] == ["issue", "comment"]
        comments.append(args[-1])
        return subprocess.CompletedProcess(args, 0, "", "")

    offered = continuation.consume_worklink_budget_continuations(tmp_path, runner=runner)
    assert len(offered) == 1
    assert continuation.consume_worklink_budget_continuations(tmp_path, runner=runner) == offered
    assert comments == []
    assert continuation.consume_worklink_budget_continuations(
        tmp_path, runner=runner, delivery_receipt_exists=lambda key: key == offered[0].delivery_key,
    ) == []
    assert len(comments) == 1 and "key" in comments[0]
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["actioned_at"] and payload["external_comment"]["posted"] is True
    assert continuation.consume_worklink_budget_continuations(tmp_path, runner=runner) == []
    assert len(comments) == 1


def test_merge_closure_rejects_ambiguous_completion() -> None:
    assert merge_closure.parse_completion_reference(
        "Closes chainlink #1949.\n\nImplemented.", expected_issue_id=1949,
    ).qualifies
    assert not merge_closure.parse_completion_reference(
        "Closes chainlink #1949.\n\nStill incomplete.", expected_issue_id=1949,
    ).qualifies
    assert not merge_closure.parse_completion_reference(
        "Closes chainlink #1950.\n\nImplemented.", expected_issue_id=1949,
    ).qualifies


def test_merge_reconciliation_audits_before_closing_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    home.mkdir()
    repo.mkdir()
    (home / "worklink.yaml").write_text("repository: example/project\n", encoding="utf-8")
    (home / "repositories.yaml").write_text(
        "repositories:\n  - slug: example/project\n"
        f"    root: {repo}\n    mode: rw\n"
        "    origin: https://github.com/example/project.git\n    base_branch: main\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("WORKLINK_REPO", str(repo))
    monkeypatch.delenv("MIMIR_WORKLINK_REPO", raising=False)
    evidence = home / "state" / "worklink" / "evidence" / "1949-1.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text(json.dumps({
        "issue": 1949, "attempt": 1, "status": "completed",
        "pr_url": "https://github.com/example/project/pull/42", "base_ref": "main",
    }), encoding="utf-8")
    calls: list[str] = []
    status = "open"
    labels = {"worklink:review"}
    comments: list[str] = []

    def result(value: object = "") -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, json.dumps(value) if not isinstance(value, str) else value, "")

    def tracker(args: list[str]) -> subprocess.CompletedProcess[str]:
        nonlocal status
        command = args[1:]
        if command[:2] == ["issue", "list"]:
            return result([{"id": 1949}] if status == "open" else [])
        if command[:2] == ["issue", "show"]:
            return result({
                "id": 1949, "number": 1949, "status": status, "closed": status == "closed",
                "labels": sorted(labels),
                "comments": comments, "parent_id": None,
            })
        if command[:2] in (["issue", "comment"], ["issue", "close"], ["issue", "unlabel"]):
            calls.append(command[1])
            if command[1] == "comment":
                comments.append(command[3])
            if command[1] == "close":
                status = "closed"
            if command[1] == "unlabel":
                labels.discard(command[3])
            return result()
        raise AssertionError(args)

    def git(args: list[str]) -> subprocess.CompletedProcess[str]:
        if args[-2:] == ["rev-parse", "--show-toplevel"]:
            return result(str(repo))
        if args[-3:] == ["remote", "get-url", "origin"]:
            return result("https://github.com/example/project.git")
        raise AssertionError(args)

    def forge(args: list[str]) -> subprocess.CompletedProcess[str]:
        assert args == ["gh", "api", "repos/example/project/pulls/42"]
        return result({
            "number": 42, "html_url": "https://github.com/example/project/pull/42",
            "body": "Closes chainlink #1949.\n\nImplemented.", "state": "closed",
            "merged": True, "merged_at": "2026-09-20T12:00:00Z",
            "merge_commit_sha": "abcdef1234567890",
            "base": {"repo": {"full_name": "example/project"}, "ref": "main"},
        })

    sweep = lambda: merge_closure.reconcile_merged_leaves(
        home, chainlink_runner=tracker, gh_runner=forge, git_runner=git,
    )
    assert [outcome.issue_id for outcome in sweep()] == [1949]
    assert calls == ["comment", "close", "unlabel"]
    assert comments[0].startswith("WORKLINK_CLOSED v1 ")
    assert sweep() == []
    assert calls == ["comment", "close", "unlabel"]


def test_poller_relevance_refuses_stale_occurrence(tmp_path: Path) -> None:
    state = tmp_path / poller_hooks.STATE_FILE
    state.write_text(json.dumps({"issues": {"1949": {
        "active": True, "signature": "original", "occurrence_id": "first",
    }}}), encoding="utf-8")
    event = SimpleNamespace(
        extra={"items": [{
            "issue_id": 1949, "error_signature": "original", "failure_occurrence_id": "second",
        }]},
    )
    assert asyncio.run(poller_hooks.recovery_relevance_check(tmp_path)(event)) is False


def test_retained_scope_refuses_untrusted_service() -> None:
    result = derive_retained_factory_scope(
        SimpleNamespace(service_principal="untrusted"),
        SimpleNamespace(canonical="poller:worklink-ready-queue", trigger="poller", authority_profile="github"),
    )
    assert result.scope is None
    assert result.refusal_reason == "service is not the Worklink ready queue"


def test_retained_effect_lease_accepts_only_current_dead_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = RetainedFactoryScope(
        issue_id=1949, signature="failure", occurrence_id="occurrence",
        run_id="run", attempt=1, session="session", repository="owner/repo",
        branch="branch", sandbox=str(tmp_path / "sandbox"),
    )
    record = SimpleNamespace(**{
        key: getattr(scope, key) for key in (
            "issue_id", "run_id", "attempt", "session", "repository", "branch", "sandbox"
        )
    })

    @contextmanager
    def acquired(*_args):
        yield True

    monkeypatch.setattr(retained_scope, "factory_checkout_interlock", acquired)
    monkeypatch.setattr(retained_scope, "factory_issue_resource_lock", acquired)
    monkeypatch.setattr(retained_scope, "current_failure_record", lambda *_: {
        "signature": scope.signature, "occurrence_id": scope.occurrence_id,
    })
    monkeypatch.setattr(retained_scope, "load_factory_records_for_issue", lambda *_: [record])
    monkeypatch.setattr(retained_scope, "factory_process_is_alive", lambda _: False)
    monkeypatch.setattr(retained_scope, "factory_process_is_verified_dead", lambda _: True)
    monkeypatch.setattr(retained_scope, "_factory_session_lock_is_fresh", lambda _: False)

    with retained_scope.retained_factory_effect_lease(tmp_path, scope) as result:
        assert result.scope is scope
    monkeypatch.setattr(retained_scope, "_factory_session_lock_is_fresh", lambda _: True)
    with retained_scope.retained_factory_effect_lease(tmp_path, scope) as result:
        assert result.scope is None
        assert result.refusal_reason == "factory session lock is fresh"


def test_poller_receipt_keeps_only_live_unnotified_incident(tmp_path: Path) -> None:
    root = dispatch_failure_state_dir(tmp_path)
    root.mkdir(parents=True)
    state = {"version": 1, "issues": {"1949": {
        "issue_id": 1949, "active": True, "signature": "failure",
        "occurrence_id": "occurrence", "notified_signatures": [],
    }}}
    (root / poller_hooks.STATE_FILE).write_text(json.dumps(state), encoding="utf-8")
    receipts = []
    poller_hooks.report_live_delivery_receipts(root, tmp_path, receipts.append)
    key = hashlib.sha256(b"worklink-run-failure:1949:failure:occurrence").hexdigest()
    assert receipts == [{key}]
    state["issues"]["1949"]["notified_signatures"] = ["failure"]
    (root / poller_hooks.STATE_FILE).write_text(json.dumps(state), encoding="utf-8")
    poller_hooks.report_live_delivery_receipts(root, tmp_path, receipts.append)
    assert receipts[-1] == set()
