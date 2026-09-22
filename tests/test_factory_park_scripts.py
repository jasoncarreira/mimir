"""Tests for the hand-park and resume operator scripts.

Both failures these guard against were observed on chainlink #1783 attempt 9:
the snapshot published under the sandbox instead of the checkout, and mimir's
retained record left reading ``running`` so cleanup pruned a parked run.
"""

from __future__ import annotations

import importlib.util
import os
import stat
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


park = _load("factory_park")
resume = _load("factory_resume")


def _parked_dir(tmp_path: Path) -> Path:
    """The snapshot root, under a checkout that exists as it would in reality."""
    checkout = tmp_path / "checkout"
    checkout.mkdir(exist_ok=True)
    return checkout / ".factory" / ".parked"


def _plane(root: Path) -> Path:
    """Build a control plane resembling a live run's."""
    plane = root
    plane.mkdir(parents=True)
    (plane / "run.json").write_text('{"status": "running"}')
    (plane / "WORKFLOW.md").write_text("contract")
    (plane / "factory.lock").write_text("heartbeat-1")
    (plane / "artifacts").mkdir()
    (plane / "artifacts" / "technical-brief.md").write_text("brief")
    (plane / "reviews").mkdir()
    (plane / "reviews" / "spec-writer.json").write_text('{"verdict": "APPROVE"}')
    return plane


class TestOperatorRoot:
    def test_snapshot_root_is_the_checkout_not_the_sandbox(self, tmp_path: Path) -> None:
        """The factory reads <checkout>/.factory/.parked, one level above the sandbox.

        Publishing under the sandbox's own .factory produces a byte-correct
        snapshot that ``observedParkSnapshot`` never acknowledges.
        """
        checkout = tmp_path / "checkout"
        sandbox = checkout / ".factory-sandboxes" / "chainlink-1"
        sandbox.mkdir(parents=True)

        assert park.operator_root(sandbox) == checkout.resolve()
        assert park.operator_root(sandbox) != sandbox

    def test_sandbox_of_another_shape_is_refused_rather_than_guessed(self, tmp_path: Path) -> None:
        stray = tmp_path / "somewhere" / "chainlink-1"
        stray.mkdir(parents=True)
        with pytest.raises(park.ParkError, match="refusing to guess"):
            park.operator_root(stray)


class TestPublishSnapshot:
    def test_publishes_a_verified_copy_excluding_only_the_plane_root_lock(self, tmp_path: Path) -> None:
        plane = _plane(tmp_path / "plane")
        parked = _parked_dir(tmp_path)

        published = park.publish_snapshot(plane, parked, "chainlink-1")

        assert published == parked / "chainlink-1"
        assert (published / "artifacts" / "technical-brief.md").read_text() == "brief"
        assert (published / "reviews" / "spec-writer.json").read_text() == '{"verdict": "APPROVE"}'
        # The lock is copied, but excluded from the comparison that gates publication.
        assert (published / "factory.lock").exists()

    def test_a_heartbeat_landing_mid_copy_does_not_fail_publication(self, tmp_path: Path, monkeypatch) -> None:
        """Only the plane-root factory.lock is excluded, and that is why."""
        plane = _plane(tmp_path / "plane")
        parked = _parked_dir(tmp_path)

        real_inventory = park.plane_inventory
        calls = {"n": 0}

        def ticking_inventory(root: Path):
            calls["n"] += 1
            if calls["n"] == 1:
                (plane / "factory.lock").write_text("heartbeat-2")
            return real_inventory(root)

        monkeypatch.setattr(park, "plane_inventory", ticking_inventory)
        published = park.publish_snapshot(plane, parked, "chainlink-1")
        assert published.is_dir()

    def test_a_nested_factory_lock_is_run_state_and_must_match(self, tmp_path: Path, monkeypatch) -> None:
        plane = _plane(tmp_path / "plane")
        (plane / "artifacts" / "factory.lock").write_text("nested-run-state")
        parked = _parked_dir(tmp_path)

        real_inventory = park.plane_inventory
        calls = {"n": 0}

        def corrupting_inventory(root: Path):
            calls["n"] += 1
            if calls["n"] == 1:
                (plane / "artifacts" / "factory.lock").write_text("changed-after-copy")
            return real_inventory(root)

        monkeypatch.setattr(park, "plane_inventory", corrupting_inventory)
        with pytest.raises(park.ParkError, match="inventory mismatch"):
            park.publish_snapshot(plane, parked, "chainlink-1")
        assert not (parked / "chainlink-1").exists()
        assert not (parked / ".staging-chainlink-1").exists()

    def test_nothing_is_published_when_verification_fails(self, tmp_path: Path, monkeypatch) -> None:
        plane = _plane(tmp_path / "plane")
        parked = _parked_dir(tmp_path)

        monkeypatch.setattr(park, "plane_inventory", lambda root: [("differs", "file", 0o644, str(root))])
        with pytest.raises(park.ParkError, match="nothing published"):
            park.publish_snapshot(plane, parked, "chainlink-1")
        assert not (parked / "chainlink-1").exists()

    def test_a_residual_staging_tree_refuses_rather_than_overwriting(self, tmp_path: Path) -> None:
        plane = _plane(tmp_path / "plane")
        parked = _parked_dir(tmp_path)
        (parked / ".staging-chainlink-1").mkdir(parents=True)

        with pytest.raises(park.ParkError, match="residual staging"):
            park.publish_snapshot(plane, parked, "chainlink-1")

    def test_republishing_replaces_the_previous_snapshot(self, tmp_path: Path) -> None:
        plane = _plane(tmp_path / "plane")
        parked = _parked_dir(tmp_path)
        park.publish_snapshot(plane, parked, "chainlink-1")

        (plane / "artifacts" / "technical-brief.md").write_text("revised brief")
        published = park.publish_snapshot(plane, parked, "chainlink-1")

        assert (published / "artifacts" / "technical-brief.md").read_text() == "revised brief"
        assert not (parked / ".prior-chainlink-1").exists()

    def test_symlinks_are_preserved_as_symlinks(self, tmp_path: Path) -> None:
        plane = _plane(tmp_path / "plane")
        (plane / "artifacts" / "latest.md").symlink_to("technical-brief.md")
        parked = _parked_dir(tmp_path)

        published = park.publish_snapshot(plane, parked, "chainlink-1")
        assert (published / "artifacts" / "latest.md").is_symlink()
        assert os.readlink(published / "artifacts" / "latest.md") == "technical-brief.md"

    def test_refuses_to_write_through_a_symlinked_parent(self, tmp_path: Path) -> None:
        plane = _plane(tmp_path / "plane")
        control = tmp_path / "checkout" / ".factory"
        control.parent.mkdir(parents=True, exist_ok=True)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        control.symlink_to(elsewhere)

        with pytest.raises(park.ParkError, match="symlinked parent"):
            park.publish_snapshot(plane, control / ".parked", "chainlink-1")


class TestPlaneInventory:
    def test_records_mode_so_a_permission_change_is_a_mismatch(self, tmp_path: Path) -> None:
        plane = _plane(tmp_path / "plane")
        before = park.plane_inventory(plane)
        (plane / "run.json").chmod(0o600)
        after = park.plane_inventory(plane)
        assert before != after

    def test_records_content_so_an_edit_is_a_mismatch(self, tmp_path: Path) -> None:
        plane = _plane(tmp_path / "plane")
        before = park.plane_inventory(plane)
        (plane / "run.json").write_text('{"status": "needs-human"}')
        assert park.plane_inventory(plane) != before


class TestResumePreconditions:
    def test_a_swept_sandbox_is_refused_with_the_reason(self, tmp_path: Path) -> None:
        argv = [
            "--run-id", "chainlink-1", "--sandbox", str(tmp_path / "gone"),
            "--home", str(tmp_path), "--launcher", str(tmp_path / "factory.js"),
            "--session", "ses_new",
        ]
        with pytest.raises(resume.ResumeError, match="not resumable"):
            resume.main(argv)
