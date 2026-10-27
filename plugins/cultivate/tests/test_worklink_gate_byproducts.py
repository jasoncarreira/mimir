"""Publication uses the builder's tree, not files written by the test gate."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mimir_cultivate.worklink import orchestrator
from mimir_cultivate.worklink.backends.base import RawResult
from mimir_cultivate.worklink.backends.registry import WorklinkConfig, WorklinkDefaults
from mimir_cultivate.worklink.checkout import CheckoutLease
from mimir_cultivate.worklink.claims import ClaimRecord
from mimir_cultivate.worklink.compute import ComputeResult


def _run(args, *, cwd=None, timeout=None, text=True):
    return subprocess.run(
        args, cwd=cwd, timeout=timeout, shell=isinstance(args, str),
        text=text, capture_output=True, check=False,
    )


def _git(repo: Path, *args: str) -> str:
    result = _run(["git", "-C", str(repo), *args])
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


class _Release:
    label_transition_applied = False
    retain_for_recovery = False

    def __call__(self):
        return True


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [
    "gate_output", "gate_rewrite", "gate_delete", "gate_stage", "gate_tracked_output",
    "gate_tracked_delete", "gate_tracked_mode", "gate_tracked_rename",
    "gate_untracked_rewrite", "late_rewrite", "late_tracked_rewrite",
    "bulk", "bulk_distributed", "source_files", "tracked_delete", "tracked_rename",
])
async def test_finalize_publishes_only_builder_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str,
) -> None:
    repo = tmp_path / "checkout"
    repo.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    _git(repo, "init", "-b", "feature/plugins")
    _git(repo, "config", "user.name", "Worklink Test")
    _git(repo, "config", "user.email", "test@example.test")
    (repo / "README.md").write_text("before\n")
    (repo / "old.txt").write_text("old\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    _git(repo, "switch", "-c", "issue/2039-a1")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "README.md").write_text("builder\n")
    gate = "true"
    if case == "gate_output":
        gate = "mkdir -p target-test-support && printf output > target-test-support/artifact"
    elif case == "gate_rewrite":
        gate = "printf gate > README.md"
    elif case == "gate_delete":
        gate = "rm README.md"
    elif case == "gate_stage":
        gate = "git add README.md"
    elif case == "gate_tracked_output":
        gate = "printf gate > old.txt"
    elif case == "gate_tracked_delete":
        gate = "rm old.txt"
    elif case == "gate_tracked_mode":
        _git(repo, "config", "core.filemode", "true")
        (repo / "old.txt").chmod(0o644)
        gate = "chmod +x old.txt"
    elif case == "gate_tracked_rename":
        gate = "mv old.txt renamed.txt"
    elif case == "gate_untracked_rewrite":
        (repo / "new.txt").write_text("builder\n")
        gate = "printf gate > new.txt"
    elif case in {"late_rewrite", "late_tracked_rewrite"}:
        target = "README.md" if case == "late_rewrite" else "old.txt"
        gate = f"if test -f target-test-support/seen; then printf gate > {target}; " \
               "else mkdir -p target-test-support; touch target-test-support/seen; fi"
    elif case in {"bulk", "bulk_distributed"}:
        directories = {"build-output": 4} if case == "bulk" else {"build-a": 2, "build-b": 2}
        for name, count in directories.items():
            output = repo / name
            output.mkdir()
            for index in range(count):
                (output / f"{index}.bin").write_text("output")
    elif case == "source_files":
        source = repo / "src"
        source.mkdir()
        for index in range(2):
            (source / f"{index}.py").write_text(f"value = {index}\n")
    elif case == "tracked_delete":
        (repo / "old.txt").unlink()
    elif case == "tracked_rename":
        (repo / "old.txt").rename(repo / "new.txt")

    monkeypatch.setattr(orchestrator, "_with_outside_checkout_detection", lambda result, **kw: result)
    monkeypatch.setattr(orchestrator, "_git_push", Mock())
    monkeypatch.setattr(orchestrator, "_open_pr", lambda *a, **kw: "https://example.test/pr/2039")
    monkeypatch.setattr(orchestrator, "_comment_evidence", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "_cleanup_checkout_after_transition", lambda *a, **kw: None)
    claims = SimpleNamespace(transition_issue=Mock())

    async def interpret(*args):
        return RawResult(0, None, "success", None)

    started = datetime.now(UTC)
    issue = orchestrator.IssueContext(2039, "title", "body", set())
    result = await orchestrator.WorklinkRunner(home=home, repo=repo)._finalize(
        issue=issue, claims=claims, claim_record=ClaimRecord(2039, 1, "agent", started),
        attempt=1, config=WorklinkConfig(defaults=WorklinkDefaults(max_new_untracked_files=3)),
        backend=SimpleNamespace(name="feature_factory", interpret=interpret),
        compute=None, compute_result=ComputeResult(0, "", ""), order=None,
        lease=CheckoutLease(2039, 1, repo, repo, "issue/2039-a1", "feature/plugins"),
        spec=SimpleNamespace(backend_config={}, base_ref="feature/plugins", timeout_s=30),
        started=started, test_cmd=gate, root_dirty_before=(), runner=_run,
        terminal_release=_Release(),
    )
    evidence = json.loads(result.evidence_path.read_text())
    late_changes = {"late_rewrite", "late_tracked_rewrite"}
    gate_changes = {
        "gate_rewrite", "gate_delete", "gate_stage", "gate_tracked_output",
        "gate_tracked_delete", "gate_tracked_mode", "gate_tracked_rename",
        "gate_untracked_rewrite", *late_changes,
    }
    if case in gate_changes | {"bulk", "bulk_distributed"}:
        assert result.status == "blocked", result.reason
        assert result.pr_url is None
        if case not in late_changes:
            assert _git(repo, "rev-parse", "HEAD") == base
        else:
            assert _git(repo, "show", "HEAD:README.md") == "builder"
        if case in gate_changes:
            assert "gate_changed_builder_content" in result.reason
            if case.startswith("gate_tracked_"):
                assert "old.txt" in result.reason
                assert "old.txt" not in evidence["gate_byproducts_top_level"]
        else:
            if case == "bulk":
                assert "build-output: 4" in result.reason
            else:
                assert "build-a: 2" in result.reason
                assert "build-b: 2" in result.reason
            assert "defaults.max_new_untracked_files" in result.reason
        orchestrator._git_push.assert_not_called()
    else:
        assert result.status == "completed", result.reason
        committed = set(_git(repo, "diff", "--name-only", "feature/plugins...HEAD").splitlines())
        expected = {"README.md"}
        if case == "source_files":
            expected |= {"src/0.py", "src/1.py"}
        if case == "tracked_delete":
            expected.add("old.txt")
        if case == "tracked_rename":
            expected.add("new.txt")
        assert committed == expected
        assert set(evidence["files_changed"]) == expected
        if case == "gate_output":
            assert evidence["gate_byproducts_count"] == 1
            assert evidence["gate_byproducts_top_level"] == ["target-test-support"]
            assert "target-test-support" not in " ".join(evidence["files_changed"])


def test_untracked_limit_config(tmp_path: Path) -> None:
    path = tmp_path / "worklink.yaml"
    path.write_text("defaults:\n  max_new_untracked_files: 7\n")
    assert WorklinkConfig.load(path).defaults.max_new_untracked_files == 7
    assert WorklinkConfig().defaults.max_new_untracked_files == 200
    path.write_text("defaults:\n  max_new_untracked_files: -1\n")
    with pytest.raises(ValueError, match="max_new_untracked_files"):
        WorklinkConfig.load(path)
