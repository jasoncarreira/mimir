from __future__ import annotations

from dataclasses import replace
import hashlib
import os
from pathlib import Path
import subprocess
from typing import Mapping, Sequence
import uuid

import pytest

from mimir.repository_config import RepositoryConfig, RepositoryInventory
from mimir.worklink import checkout_operations as operations
from mimir.worklink.checkout_operations import (
    CheckoutConflictError,
    CheckoutOperationError,
    RetainedCheckoutOperations,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["/usr/bin/git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repository(tmp_path: Path) -> Path:
    repo = tmp_path / "checkout"
    repo.mkdir()
    subprocess.run(["/usr/bin/git", "init", "-q", "-b", "repair"], cwd=repo, check=True)
    _git(repo, "config", "user.name", "Worklink Test")
    _git(repo, "config", "user.email", "worklink@example.invalid")
    (repo / "app.py").write_text("answer = 1\n", encoding="utf-8")
    (repo / "nested").mkdir()
    (repo / "nested" / "note.txt").write_text("before\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def _open(
    repo: Path,
    *,
    runner: operations.CommandRunner | None = None,
    revalidate=lambda: None,
) -> RetainedCheckoutOperations:
    observed = repo.stat(follow_symlinks=False)
    return RetainedCheckoutOperations.capture(
        repo,
        expected_device=observed.st_dev,
        expected_inode=observed.st_ino,
        branch="repair",
        repository_slug="acme/widgets",
        issue_id=1783,
        attempt=7,
        revalidate=revalidate,
        runner=runner,
    )


def _inventory(repo: Path, command: str) -> RepositoryInventory:
    return RepositoryInventory(
        repositories=(RepositoryConfig(
            slug="acme/widgets",
            root=repo,
            mode="rw",
            origin="https://github.com/acme/widgets.git",
            base_branch="main",
            test_command=command,
        ),),
        declared=True,
    )


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "",
        ".",
        "..",
        "../outside",
        "nested/../app.py",
        "nested/./note.txt",
        "nested//note.txt",
        ".git/config",
        "nested/.git/config",
        "nested\\note.txt",
        "bad\x00name",
    ],
)
def test_relative_paths_reject_escape_and_noncanonical_forms(tmp_path: Path, path: str) -> None:
    checkout = _open(_repository(tmp_path))

    with pytest.raises(ValueError):
        checkout.read_file(path)


def test_reads_are_bounded_and_refuse_symlinks(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    (repo / "large.txt").write_bytes(b"a" * (operations.MAX_READ_BYTES + 10))
    (repo / "outside.txt").write_text("outside", encoding="utf-8")
    (repo / "link").symlink_to(repo / "outside.txt")
    checkout = _open(repo)

    result = checkout.read_file("large.txt", offset=3, limit=17)

    assert result.content == b"a" * 17
    assert result.size == operations.MAX_READ_BYTES + 10
    assert result.complete is False
    with pytest.raises(OSError):
        checkout.read_file("link")
    with pytest.raises(CheckoutOperationError, match="symlink"):
        checkout.list_files()
    with pytest.raises(ValueError):
        checkout.read_file("app.py", limit=operations.MAX_READ_BYTES + 1)


def test_checkout_directory_replacement_is_refused(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    checkout = _open(repo)
    moved = tmp_path / "original"
    repo.rename(moved)
    repo.mkdir()
    (repo / "app.py").write_text("replacement\n", encoding="utf-8")

    with pytest.raises(CheckoutConflictError, match="replaced"):
        checkout.read_file("app.py")


def _detach_nested_parent(repo: Path, tmp_path: Path) -> Path:
    detached = tmp_path / "detached-nested"
    outside = tmp_path / "outside-nested"
    outside.mkdir()
    (repo / "nested").rename(detached)
    (repo / "nested").symlink_to(outside, target_is_directory=True)
    return detached


def test_list_refuses_intermediate_directory_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repository(tmp_path)
    checkout = _open(repo)
    real_listdir = operations.os.listdir
    calls = 0

    def listdir_then_replace(path):
        nonlocal calls
        result = real_listdir(path)
        calls += 1
        if calls == 2:
            _detach_nested_parent(repo, tmp_path)
        return result

    monkeypatch.setattr(operations.os, "listdir", listdir_then_replace)

    with pytest.raises(CheckoutConflictError, match="replaced"):
        checkout.list_files()


def test_list_refuses_target_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repository(tmp_path)
    checkout = _open(repo)
    real_stat = operations.os.stat
    replaced = False

    def stat_then_replace(path, *args, **kwargs):
        nonlocal replaced
        value = real_stat(path, *args, **kwargs)
        if path == "app.py" and kwargs.get("dir_fd") is not None and not replaced:
            replaced = True
            replacement = repo / "replacement"
            replacement.write_text("answer = 1\n", encoding="utf-8")
            replacement.replace(repo / "app.py")
        return value

    monkeypatch.setattr(operations.os, "stat", stat_then_replace)

    with pytest.raises(CheckoutConflictError, match="replaced"):
        checkout.list_files()


@pytest.mark.parametrize("replacement", ["parent", "target"])
def test_read_refuses_nested_path_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    repo = _repository(tmp_path)
    checkout = _open(repo)
    real_open = operations._open_regular
    replaced = False

    def open_then_replace(parent_fd, name):
        nonlocal replaced
        descriptor = real_open(parent_fd, name)
        if not replaced:
            replaced = True
            if replacement == "parent":
                _detach_nested_parent(repo, tmp_path)
            else:
                new_file = repo / "nested" / "replacement"
                new_file.write_text("before\n", encoding="utf-8")
                new_file.replace(repo / "nested" / "note.txt")
        return descriptor

    monkeypatch.setattr(operations, "_open_regular", open_then_replace)

    with pytest.raises(CheckoutConflictError, match="replaced"):
        checkout.read_file("nested/note.txt")


def test_write_intent_precedes_atomic_mutation_and_replays_by_digest(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    checkout = _open(repo)
    path = repo / "app.py"
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    operation_id = str(uuid.uuid4())

    intent = checkout.prepare_write(
        operation_id,
        "app.py",
        "answer = 2\n",
        expected_sha256=before,
    )

    assert path.read_text(encoding="utf-8") == "answer = 1\n"
    applied = checkout.apply_file_intent(intent, content="answer = 2\n")
    replayed = checkout.apply_file_intent(intent, content="answer = 2\n")
    assert applied.outcome == "applied"
    assert replayed.outcome == "already_applied"
    assert applied.sha256 == intent.resulting_sha256
    assert path.read_text(encoding="utf-8") == "answer = 2\n"


def test_write_file_persists_intent_before_replacement(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    checkout = _open(repo)
    path = repo / "app.py"
    persisted = []

    def persist(intent) -> None:
        assert path.read_text(encoding="utf-8") == "answer = 1\n"
        persisted.append(intent)

    result = checkout.write_file(
        str(uuid.uuid4()),
        "app.py",
        "answer = 5\n",
        expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        persist_intent=persist,
    )

    assert persisted == [result.intent]
    assert path.read_text(encoding="utf-8") == "answer = 5\n"


def test_write_crash_after_replace_is_classified_as_already_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repository(tmp_path)
    checkout = _open(repo)
    before = hashlib.sha256((repo / "app.py").read_bytes()).hexdigest()
    intent = checkout.prepare_write(
        str(uuid.uuid4()), "app.py", "answer = 3\n", expected_sha256=before
    )
    real_replace = operations.os.replace
    crashed = False

    def replace_then_crash(*args, **kwargs):
        nonlocal crashed
        real_replace(*args, **kwargs)
        if not crashed:
            crashed = True
            raise RuntimeError("simulated persistence crash")

    monkeypatch.setattr(operations.os, "replace", replace_then_crash)
    with pytest.raises(RuntimeError, match="simulated"):
        checkout.apply_file_intent(intent, content="answer = 3\n")
    monkeypatch.setattr(operations.os, "replace", real_replace)

    replay = checkout.apply_file_intent(intent, content="answer = 3\n")
    assert replay.outcome == "already_applied"


def test_delete_requires_digest_and_replays_after_unlink(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    checkout = _open(repo)
    path = repo / "nested" / "note.txt"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        checkout.prepare_delete(
            str(uuid.uuid4()), "nested/note.txt", expected_sha256="absent"
        )

    intent = checkout.prepare_delete(
        str(uuid.uuid4()), "nested/note.txt", expected_sha256=digest
    )
    assert path.exists()
    assert checkout.apply_file_intent(intent).outcome == "applied"
    assert checkout.apply_file_intent(intent).outcome == "already_applied"
    assert not path.exists()


@pytest.mark.parametrize("action", ["write", "delete"])
@pytest.mark.parametrize("replacement", ["parent", "target"])
def test_replay_refuses_nested_path_replacement_before_already_applied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    replacement: str,
) -> None:
    repo = _repository(tmp_path)
    checkout = _open(repo)
    path = repo / "nested" / "note.txt"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if action == "write":
        intent = checkout.prepare_write(
            str(uuid.uuid4()),
            "nested/note.txt",
            "after\n",
            expected_sha256=digest,
        )
        checkout.apply_file_intent(intent, content="after\n")
    else:
        intent = checkout.prepare_delete(
            str(uuid.uuid4()), "nested/note.txt", expected_sha256=digest
        )
        checkout.apply_file_intent(intent)
    real_digest = operations._digest_at
    replaced = False

    def digest_then_replace(parent_fd, name, *, missing_ok):
        nonlocal replaced
        result = real_digest(parent_fd, name, missing_ok=missing_ok)
        if not replaced:
            replaced = True
            if replacement == "parent":
                _detach_nested_parent(repo, tmp_path)
            elif action == "write":
                alternate = repo / "nested" / "alternate"
                alternate.write_text("after\n", encoding="utf-8")
                alternate.replace(path)
            else:
                path.write_text("resurrected\n", encoding="utf-8")
        return result

    monkeypatch.setattr(operations, "_digest_at", digest_then_replace)

    with pytest.raises(CheckoutConflictError, match="replaced|absent"):
        checkout.apply_file_intent(
            intent, content="after\n" if action == "write" else None
        )


@pytest.mark.parametrize("action", ["write", "delete"])
@pytest.mark.parametrize("replacement", ["parent", "target"])
def test_mutation_refuses_nested_path_replacement_after_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    replacement: str,
) -> None:
    repo = _repository(tmp_path)
    checkout = _open(repo)
    path = repo / "nested" / "note.txt"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if action == "write":
        intent = checkout.prepare_write(
            str(uuid.uuid4()),
            "nested/note.txt",
            "after\n",
            expected_sha256=digest,
        )
        real_effect = operations.os.replace
        changed = False

        def replace_then_change(*args, **kwargs):
            nonlocal changed
            result = real_effect(*args, **kwargs)
            if not changed:
                changed = True
                if replacement == "parent":
                    _detach_nested_parent(repo, tmp_path)
                else:
                    alternate = repo / "nested" / "alternate"
                    alternate.write_text("after\n", encoding="utf-8")
                    real_effect(alternate, path)
            return result

        monkeypatch.setattr(operations.os, "replace", replace_then_change)
    else:
        intent = checkout.prepare_delete(
            str(uuid.uuid4()), "nested/note.txt", expected_sha256=digest
        )
        real_effect = operations.os.unlink
        changed = False

        def unlink_then_change(*args, **kwargs):
            nonlocal changed
            result = real_effect(*args, **kwargs)
            if not changed:
                changed = True
                if replacement == "parent":
                    _detach_nested_parent(repo, tmp_path)
                else:
                    path.write_text("resurrected\n", encoding="utf-8")
            return result

        monkeypatch.setattr(operations.os, "unlink", unlink_then_change)

    with pytest.raises(CheckoutConflictError, match="replaced|absent"):
        checkout.apply_file_intent(
            intent, content="after\n" if action == "write" else None
        )


def test_target_inode_replacement_during_write_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repository(tmp_path)
    checkout = _open(repo)
    path = repo / "app.py"
    intent = checkout.prepare_write(
        str(uuid.uuid4()),
        "app.py",
        "answer = 4\n",
        expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    real_verify = operations._verify_target_identity
    calls = 0

    def replace_between_checks(parent_fd, name, expected):
        nonlocal calls
        calls += 1
        real_verify(parent_fd, name, expected)
        if calls == 1:
            replacement = repo / "replacement"
            replacement.write_text("answer = 1\n", encoding="utf-8")
            replacement.replace(path)

    monkeypatch.setattr(operations, "_verify_target_identity", replace_between_checks)

    with pytest.raises(CheckoutConflictError, match="replaced"):
        checkout.apply_file_intent(intent, content="answer = 4\n")
    assert path.read_text(encoding="utf-8") == "answer = 1\n"


@pytest.mark.parametrize("action", ["write", "delete"])
def test_mutation_refuses_detached_nested_parent_before_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    repo = _repository(tmp_path)
    checkout = _open(repo)
    path = repo / "nested" / "note.txt"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if action == "write":
        intent = checkout.prepare_write(
            str(uuid.uuid4()),
            "nested/note.txt",
            "after\n",
            expected_sha256=digest,
        )
    else:
        intent = checkout.prepare_delete(
            str(uuid.uuid4()), "nested/note.txt", expected_sha256=digest
        )
    real_verify = operations._verify_target_identity
    replaced = False

    def verify_then_detach(parent_fd, name, expected):
        nonlocal replaced
        real_verify(parent_fd, name, expected)
        if not replaced:
            replaced = True
            _detach_nested_parent(repo, tmp_path)

    monkeypatch.setattr(operations, "_verify_target_identity", verify_then_detach)

    with pytest.raises(CheckoutConflictError, match="replaced"):
        checkout.apply_file_intent(
            intent, content="after\n" if action == "write" else None
        )
    assert (tmp_path / "detached-nested" / "note.txt").read_text() == "before\n"
    assert not (tmp_path / "outside-nested" / "note.txt").exists()


def test_delete_refuses_target_replacement_before_unlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repository(tmp_path)
    checkout = _open(repo)
    path = repo / "nested" / "note.txt"
    intent = checkout.prepare_delete(
        str(uuid.uuid4()),
        "nested/note.txt",
        expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    real_verify = operations._verify_target_identity
    replaced = False

    def verify_then_replace(parent_fd, name, expected):
        nonlocal replaced
        real_verify(parent_fd, name, expected)
        if not replaced:
            replaced = True
            replacement = repo / "nested" / "replacement"
            replacement.write_text("before\n", encoding="utf-8")
            replacement.replace(path)

    monkeypatch.setattr(operations, "_verify_target_identity", verify_then_replace)

    with pytest.raises(CheckoutConflictError, match="replaced"):
        checkout.apply_file_intent(intent)
    assert path.read_text(encoding="utf-8") == "before\n"


def test_write_limits_utf8_content_and_expected_absence(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    checkout = _open(repo)

    intent = checkout.prepare_write(
        str(uuid.uuid4()), "new.txt", "new\n", expected_sha256="absent"
    )
    checkout.apply_file_intent(intent, content="new\n")
    assert (repo / "new.txt").read_text(encoding="utf-8") == "new\n"
    with pytest.raises(ValueError, match="4 MiB"):
        checkout.prepare_write(
            str(uuid.uuid4()),
            "too-large.txt",
            "x" * (operations.MAX_WRITE_BYTES + 1),
            expected_sha256="absent",
        )
    with pytest.raises(ValueError, match="UTF-8"):
        checkout.prepare_write(
            str(uuid.uuid4()), "surrogate.txt", "\ud800", expected_sha256="absent"
        )


def test_tests_use_only_inventory_command_and_exact_checkout_cwd(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    command = "python -m pytest -q && true"
    calls: list[tuple[tuple[str, ...], Path, Mapping[str, str]]] = []

    def runner(
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
        output_limit: int,
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append((tuple(argv), cwd, env))
        if tuple(argv[:2]) == ("/bin/sh", "-c"):
            return subprocess.CompletedProcess(argv, 0, b"passed\n", b"")
        return operations._run_bounded(
            argv, cwd=cwd, env=env, timeout=timeout, output_limit=output_limit
        )

    checkout = _open(repo, runner=runner)
    result = checkout.run_tests(_inventory(repo, command))

    test_calls = [item for item in calls if item[0][:2] == ("/bin/sh", "-c")]
    assert len(test_calls) == 1
    assert test_calls[0][0] == ("/bin/sh", "-c", command)
    assert test_calls[0][1] == repo
    assert result.command == command
    assert result.passed
    wrong = RepositoryInventory(repositories=(replace(
        _inventory(repo, command).repositories[0], slug="other/repo"
    ),), declared=True)
    with pytest.raises(CheckoutOperationError, match="authorized rw"):
        checkout.run_tests(wrong)


def test_final_held_state_failure_prevents_write_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repository(tmp_path)
    denied = False

    def revalidate() -> None:
        if denied:
            raise CheckoutConflictError("leaf is no longer held")

    checkout = _open(repo, revalidate=revalidate)
    path = repo / "app.py"
    intent = checkout.prepare_write(
        str(uuid.uuid4()),
        "app.py",
        "answer = 8\n",
        expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    real_verify = operations._verify_target_identity

    def verify_then_deny(parent_fd, name, expected):
        nonlocal denied
        real_verify(parent_fd, name, expected)
        denied = True

    monkeypatch.setattr(operations, "_verify_target_identity", verify_then_deny)

    with pytest.raises(CheckoutConflictError, match="no longer held"):
        checkout.apply_file_intent(intent, content="answer = 8\n")
    assert path.read_text(encoding="utf-8") == "answer = 1\n"
    assert not any(item.name.startswith(".worklink-") for item in repo.iterdir())


def test_final_held_state_failure_prevents_delete_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repository(tmp_path)
    denied = False

    def revalidate() -> None:
        if denied:
            raise CheckoutConflictError("leaf is no longer held")

    checkout = _open(repo, revalidate=revalidate)
    path = repo / "app.py"
    intent = checkout.prepare_delete(
        str(uuid.uuid4()),
        "app.py",
        expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    real_verify = operations._verify_target_identity

    def verify_then_deny(parent_fd, name, expected):
        nonlocal denied
        real_verify(parent_fd, name, expected)
        denied = True

    monkeypatch.setattr(operations, "_verify_target_identity", verify_then_deny)

    with pytest.raises(CheckoutConflictError, match="no longer held"):
        checkout.apply_file_intent(intent)
    assert path.read_text(encoding="utf-8") == "answer = 1\n"


def test_final_held_state_failure_prevents_test_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repository(tmp_path)
    denied = False
    test_ran = False

    def revalidate() -> None:
        if denied:
            raise CheckoutConflictError("leaf is no longer held")

    def runner(argv, **kwargs):
        nonlocal test_ran
        if tuple(argv[:2]) == ("/bin/sh", "-c"):
            test_ran = True
        return operations._run_bounded(argv, **kwargs)

    checkout = _open(repo, revalidate=revalidate, runner=runner)
    real_candidate = checkout.candidate_tree

    def candidate_then_deny():
        nonlocal denied
        tree = real_candidate()
        denied = True
        return tree

    monkeypatch.setattr(checkout, "candidate_tree", candidate_then_deny)

    with pytest.raises(CheckoutConflictError, match="no longer held"):
        checkout.run_tests(_inventory(repo, "true"))
    assert test_ran is False


def test_test_result_is_rejected_if_command_changes_tracked_tree(tmp_path: Path) -> None:
    repo = _repository(tmp_path)

    def runner(
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
        output_limit: int,
    ) -> subprocess.CompletedProcess[bytes]:
        if tuple(argv[:2]) == ("/bin/sh", "-c"):
            (cwd / "app.py").write_text("changed by test\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        return operations._run_bounded(
            argv, cwd=cwd, env=env, timeout=timeout, output_limit=output_limit
        )

    checkout = _open(repo, runner=runner)
    with pytest.raises(CheckoutConflictError, match="tree changed while tests ran"):
        checkout.run_tests(_inventory(repo, "trusted-test"))


def test_candidate_tree_uses_temporary_index(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    (repo / "app.py").write_text("answer = 2\n", encoding="utf-8")
    checkout = _open(repo)

    tree = checkout.candidate_tree()

    assert tree != _git(repo, "rev-parse", "HEAD^{tree}")
    assert _git(repo, "diff", "--cached", "--name-only") == ""


def test_commit_requires_exact_tested_tree_and_fixed_message(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    (repo / "app.py").write_text("answer = 2\n", encoding="utf-8")

    def runner(
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
        output_limit: int,
    ) -> subprocess.CompletedProcess[bytes]:
        if tuple(argv[:2]) == ("/bin/sh", "-c"):
            return subprocess.CompletedProcess(argv, 0, b"ok\n", b"")
        return operations._run_bounded(
            argv, cwd=cwd, env=env, timeout=timeout, output_limit=output_limit
        )

    checks = 0

    def revalidate() -> None:
        nonlocal checks
        checks += 1

    checkout = _open(repo, runner=runner, revalidate=revalidate)
    tested = checkout.run_tests(_inventory(repo, "trusted-test"))
    before_commit_checks = checks
    committed = checkout.commit_tested_tree(tested)

    assert committed.outcome == "applied"
    assert committed.tree == tested.tree
    assert _git(repo, "show", "-s", "--format=%B", "HEAD") == "worklink: issue #1783"
    assert _git(repo, "rev-parse", "HEAD^{tree}") == tested.tree
    assert checks > before_commit_checks
    replay = checkout.commit_tested_tree(tested)
    assert replay.outcome == "already_applied"
    assert replay.commit == committed.commit


def test_commit_refuses_tree_changed_after_tests(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    (repo / "app.py").write_text("answer = 2\n", encoding="utf-8")
    checkout = _open(repo)
    tested = checkout.run_tests(_inventory(repo, "true"))
    (repo / "app.py").write_text("answer = 999\n", encoding="utf-8")

    with pytest.raises(CheckoutConflictError, match="tested tree"):
        checkout.commit_tested_tree(tested)
    assert _git(repo, "rev-parse", "HEAD") == tested.head


def test_final_held_state_failure_prevents_commit_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repository(tmp_path)
    (repo / "app.py").write_text("answer = 2\n", encoding="utf-8")
    denied = False

    def revalidate() -> None:
        if denied:
            raise CheckoutConflictError("leaf is no longer held")

    checkout = _open(repo, revalidate=revalidate)
    tested = checkout.run_tests(_inventory(repo, "true"))
    real_scan = checkout._scan_index_for_secrets

    def scan_then_deny(base, index_path):
        nonlocal denied
        real_scan(base, index_path)
        denied = True

    monkeypatch.setattr(checkout, "_scan_index_for_secrets", scan_then_deny)

    with pytest.raises(CheckoutConflictError, match="no longer held"):
        checkout.commit_tested_tree(tested)
    assert _git(repo, "rev-parse", "HEAD") == tested.head
    assert _git(repo, "diff", "--cached", "--name-only") == ""
    assert _git(repo, "diff", "--name-only") == "app.py"


def test_commit_scans_clean_blob_larger_than_command_output_cap(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    large = "x" * (operations.MAX_COMMAND_OUTPUT_BYTES + 64 * 1024)
    (repo / "large.txt").write_text(large, encoding="utf-8")
    checkout = _open(repo)
    tested = checkout.run_tests(_inventory(repo, "true"))

    result = checkout.commit_tested_tree(tested)

    assert result.outcome == "applied"
    assert _git(repo, "rev-parse", "HEAD^{tree}") == tested.tree


def test_commit_mandatorily_scans_staged_blobs_for_secrets(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    secret = "ghp_" + "A" * 36
    (repo / "retained.txt").write_text(f"credential={secret}\n", encoding="utf-8")
    checkout = _open(repo)
    tested = checkout.run_tests(_inventory(repo, "true"))

    with pytest.raises(CheckoutOperationError, match="secret-shaped") as raised:
        checkout.commit_tested_tree(tested)

    assert secret not in str(raised.value)
    assert _git(repo, "rev-parse", "HEAD") == tested.head


def test_commit_refuses_secret_beyond_command_output_cap(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    secret = "ghp_" + "B" * 36
    prefix = "x" * (operations.MAX_COMMAND_OUTPUT_BYTES + 4096)
    (repo / "large-secret.txt").write_text(
        prefix + secret + "\n", encoding="utf-8"
    )
    checkout = _open(repo)
    tested = checkout.run_tests(_inventory(repo, "true"))

    with pytest.raises(CheckoutOperationError, match="secret-shaped") as raised:
        checkout.commit_tested_tree(tested)

    assert secret not in str(raised.value)
    assert _git(repo, "rev-parse", "HEAD") == tested.head


def test_commit_fully_scans_large_base_blob_before_allowing_existing_match(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)
    existing = "ghp_" + "C" * 36
    prefix = "x" * (operations.MAX_COMMAND_OUTPUT_BYTES + 4096)
    path = repo / "large-base.txt"
    path.write_text(prefix + existing + "\nold\n", encoding="utf-8")
    _git(repo, "add", "large-base.txt")
    _git(repo, "commit", "-q", "-m", "large base")
    path.write_text(prefix + existing + "\nnew\n", encoding="utf-8")
    checkout = _open(repo)
    tested = checkout.run_tests(_inventory(repo, "true"))

    result = checkout.commit_tested_tree(tested)

    assert result.outcome == "applied"


def test_commit_refuses_failed_test_result(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    checkout = _open(repo)
    tested = checkout.run_tests(_inventory(repo, "false"))

    assert tested.exit_code == 1
    with pytest.raises(CheckoutOperationError, match="passing"):
        checkout.commit_tested_tree(tested)
