from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from mimir.worklink.epic_state import (
    EPIC_STATE_VERSION,
    EpicRunManifest,
    EpicSliceRecord,
    EpicStateError,
    epic_state_path,
    load_epic_state,
    load_or_init_epic_state,
    resume_epic_run,
    save_epic_state,
)


def test_load_or_init_epic_state_creates_initial_manifest(tmp_path: Path) -> None:
    manifest = load_or_init_epic_state(
        tmp_path,
        epic_id=770,
        integration_branch="worklink/epic-770",
        integration_worktree=tmp_path / "worktrees" / "epic-770",
        base_ref="origin/main",
        slice_ids=(771, 772),
    )

    assert manifest.epic_id == 770
    assert manifest.integration_branch == "worklink/epic-770"
    assert manifest.integration_worktree == str(tmp_path / "worktrees" / "epic-770")
    assert manifest.base_ref == "origin/main"
    assert manifest.phase == "decompose"
    assert manifest.status == "running"
    assert manifest.slices == [
        EpicSliceRecord(id=771),
        EpicSliceRecord(id=772),
    ]
    assert epic_state_path(tmp_path, 770).exists()

    existing = load_or_init_epic_state(
        tmp_path,
        epic_id=770,
        integration_branch="ignored",
        integration_worktree="ignored",
        base_ref="ignored",
        slice_ids=(999,),
    )
    assert existing == manifest


def test_epic_state_atomic_round_trip_ignores_stray_temp_file(tmp_path: Path) -> None:
    manifest = EpicRunManifest(
        epic_id=770,
        integration_branch="worklink/epic-770",
        integration_worktree="/tmp/epic-770",
        base_ref="main",
        phase="build",
        slices=[
            EpicSliceRecord(
                id=771,
                status="review",
                attempts=2,
                evidence_ref="evidence/771-2.json",
                review_ref="review/771-2.md",
            ),
            EpicSliceRecord(id=772, status="merged", attempts=1, merge_commit="abc123"),
        ],
    )

    path = save_epic_state(tmp_path, manifest)
    (path.parent / f".{path.name}.crashed.tmp").write_text("{half", encoding="utf-8")

    loaded = load_epic_state(tmp_path, 770)

    assert loaded == manifest
    assert (
        json.loads(path.read_text(encoding="utf-8"))["slices"][0]["status"] == "review"
    )


def test_present_corrupt_manifest_is_not_silently_reinitialized(tmp_path: Path) -> None:
    path = epic_state_path(tmp_path, 770)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(EpicStateError, match="invalid JSON"):
        load_epic_state(tmp_path, 770)

    with pytest.raises(EpicStateError, match="invalid JSON"):
        load_or_init_epic_state(
            tmp_path,
            epic_id=770,
            integration_branch="new-branch",
            integration_worktree="new-worktree",
            base_ref="main",
            slice_ids=(999,),
        )

    assert path.read_text(encoding="utf-8") == "{not json"


def test_present_invalid_manifest_is_not_silently_reinitialized(tmp_path: Path) -> None:
    path = epic_state_path(tmp_path, 770)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": EPIC_STATE_VERSION, "epic_id": 770}), encoding="utf-8"
    )

    with pytest.raises(EpicStateError, match="invalid epic manifest"):
        load_or_init_epic_state(
            tmp_path,
            epic_id=770,
            integration_branch="new-branch",
            integration_worktree="new-worktree",
            base_ref="main",
        )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {"version": EPIC_STATE_VERSION, "epic_id": 770}


def test_unknown_manifest_version_is_not_silently_reinitialized(tmp_path: Path) -> None:
    manifest = EpicRunManifest(
        epic_id=770,
        integration_branch="worklink/epic-770",
        integration_worktree="/tmp/epic-770",
        base_ref="main",
        phase="build",
        slices=[],
        version=EPIC_STATE_VERSION + 1,
    )
    path = epic_state_path(tmp_path, 770)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest.to_json()), encoding="utf-8")

    with pytest.raises(EpicStateError, match="unsupported epic manifest version"):
        load_or_init_epic_state(
            tmp_path,
            epic_id=770,
            integration_branch="new-branch",
            integration_worktree="new-worktree",
            base_ref="main",
        )

    assert (
        json.loads(path.read_text(encoding="utf-8"))["version"]
        == EPIC_STATE_VERSION + 1
    )


def test_resume_point_skips_merged_slices_in_partial_manifest() -> None:
    manifest = EpicRunManifest(
        epic_id=770,
        integration_branch="worklink/epic-770",
        integration_worktree="/tmp/epic-770",
        base_ref="main",
        phase="build",
        slices=[
            EpicSliceRecord(id=771, status="merged", attempts=1, merge_commit="abc123"),
            EpicSliceRecord(id=772, status="running", attempts=2),
            EpicSliceRecord(id=773, status="pending"),
        ],
        status="partial",
    )

    point = resume_epic_run(manifest)

    assert point.phase == "build"
    assert point.slice_id == 772
    assert point.slice_status == "running"
    assert point.overall_status == "partial"
    assert point.complete is False


def test_resume_point_skips_terminally_blocked_slices() -> None:
    manifest = EpicRunManifest(
        epic_id=770,
        integration_branch="worklink/epic-770",
        integration_worktree="/tmp/epic-770",
        base_ref="main",
        phase="build",
        slices=[
            EpicSliceRecord(id=771, status="merged", attempts=1, merge_commit="abc123"),
            EpicSliceRecord(id=772, status="blocked", attempts=3),
        ],
        status="partial",
    )

    point = resume_epic_run(manifest)

    assert point.phase == "integrate"
    assert point.slice_id is None
    assert point.slice_status is None
    assert point.overall_status == "partial"
    assert point.complete is False


def test_resume_point_reports_later_phases_and_completion() -> None:
    merged = EpicRunManifest(
        epic_id=770,
        integration_branch="worklink/epic-770",
        integration_worktree="/tmp/epic-770",
        base_ref="main",
        phase="integrate",
        slices=[
            EpicSliceRecord(id=771, status="merged", merge_commit="abc123"),
            EpicSliceRecord(id=772, status="merged", merge_commit="def456"),
        ],
    )

    assert resume_epic_run(merged).phase == "integrate"
    assert resume_epic_run(replace(merged, phase="pr")).phase == "pr"
    completed = replace(merged, phase="pr", status="completed")
    point = resume_epic_run(completed)
    assert point.complete is True
    assert point.phase is None


def test_manifest_tracks_orchestration_not_chainlink_leaf_status(
    tmp_path: Path,
) -> None:
    manifest = EpicRunManifest(
        epic_id=770,
        integration_branch="worklink/epic-770",
        integration_worktree="/tmp/epic-770",
        base_ref="main",
        phase="build",
        slices=[
            EpicSliceRecord(
                id=771,
                status="review",
                attempts=1,
                evidence_ref="evidence/771.json",
                review_ref="reviews/771.md",
            )
        ],
    )

    path = save_epic_state(tmp_path, manifest)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert "chainlink_status" not in payload
    assert "leaf_status" not in payload["slices"][0]
    assert payload["slices"][0]["status"] == "review"
    assert load_epic_state(tmp_path, 770) == manifest
