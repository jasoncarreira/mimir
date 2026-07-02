"""Durable Worklink integrated-epic run manifests.

The manifest is the controller's crash-recovery control plane for an
integrated epic run. Chainlink remains the source of truth for each leaf issue's
operator-visible STATUS labels; this file only records orchestration state that
Chainlink labels do not carry: integration git coordinates, wave/slice progress,
evidence/review refs, retries, and merge commits.

State lives at ``<home>/state/worklink/epics/<epic_id>.json`` and is written
with a same-directory temporary file followed by ``os.replace`` so a crash
mid-write cannot leave a half-written manifest at the canonical path.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Literal, Mapping

EPIC_STATE_VERSION = 1

EpicPhase = Literal["decompose", "build", "integrate", "pr"]
EpicSliceStatus = Literal["pending", "running", "review", "merged", "blocked"]
EpicOverallStatus = Literal["running", "completed", "blocked", "partial", "needs-human"]

EPIC_PHASES: set[str] = {"decompose", "build", "integrate", "pr"}
EPIC_SLICE_STATUSES: set[str] = {"pending", "running", "review", "merged", "blocked"}
EPIC_OVERALL_STATUSES: set[str] = {
    "running",
    "completed",
    "blocked",
    "partial",
    "needs-human",
}


@dataclass(frozen=True)
class EpicSliceRecord:
    """Orchestration record for one decomposed leaf slice.

    ``status`` is a Worklink epic-run state, not Chainlink's leaf STATUS label.
    The Chainlink issue remains authoritative for the leaf's public lifecycle.
    """

    id: int
    status: EpicSliceStatus = "pending"
    attempts: int = 0
    evidence_ref: str | None = None
    review_ref: str | None = None
    merge_commit: str | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: Any) -> "EpicSliceRecord":
        if not isinstance(data, Mapping):
            raise TypeError("epic slice record must be a JSON object")
        status = str(data.get("status", "pending"))
        if status not in EPIC_SLICE_STATUSES:
            raise ValueError(f"unknown epic slice status: {status}")
        return cls(
            id=int(data["id"]),
            status=status,  # type: ignore[arg-type]
            attempts=int(data.get("attempts") or 0),
            evidence_ref=_optional_str(data.get("evidence_ref")),
            review_ref=_optional_str(data.get("review_ref")),
            merge_commit=_optional_str(data.get("merge_commit")),
        )


@dataclass(frozen=True)
class EpicRunManifest:
    """Manifest sufficient to resume an integrated epic run."""

    epic_id: int
    integration_branch: str
    integration_worktree: str
    base_ref: str
    phase: EpicPhase
    slices: list[EpicSliceRecord]
    status: EpicOverallStatus = "running"
    version: int = EPIC_STATE_VERSION

    def to_json(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "slices": [slice_record.to_json() for slice_record in self.slices],
        }

    @classmethod
    def from_json(cls, data: Any) -> "EpicRunManifest":
        if not isinstance(data, Mapping):
            raise TypeError("epic run manifest must be a JSON object")
        phase = str(data["phase"])
        if phase not in EPIC_PHASES:
            raise ValueError(f"unknown epic phase: {phase}")
        status = str(data.get("status") or "running")
        if status not in EPIC_OVERALL_STATUSES:
            raise ValueError(f"unknown epic overall status: {status}")
        slices_data = data.get("slices") or []
        if not isinstance(slices_data, list):
            raise TypeError("epic manifest slices must be a list")
        return cls(
            epic_id=int(data["epic_id"]),
            integration_branch=str(data["integration_branch"]),
            integration_worktree=str(data["integration_worktree"]),
            base_ref=str(data["base_ref"]),
            phase=phase,  # type: ignore[arg-type]
            slices=[EpicSliceRecord.from_json(item) for item in slices_data],
            status=status,  # type: ignore[arg-type]
            version=int(data.get("version") or EPIC_STATE_VERSION),
        )


@dataclass(frozen=True)
class EpicResumePoint:
    """First incomplete epic phase, with a slice id when build work remains."""

    phase: EpicPhase | None
    slice_id: int | None = None
    slice_status: EpicSliceStatus | None = None
    complete: bool = False
    overall_status: EpicOverallStatus = "running"


def epics_dir(home: Path) -> Path:
    return home / "state" / "worklink" / "epics"


def epic_state_path(home: Path, epic_id: int) -> Path:
    return epics_dir(home) / f"{epic_id}.json"


def save_epic_state(home: Path, manifest: EpicRunManifest) -> Path:
    """Persist ``manifest`` atomically via temp file + rename."""

    path = epic_state_path(home, manifest.epic_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(manifest.to_json(), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return path


def load_epic_state(home: Path, epic_id: int) -> EpicRunManifest | None:
    path = epic_state_path(home, epic_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return EpicRunManifest.from_json(data)
    except (KeyError, TypeError, ValueError):
        return None


def load_or_init_epic_state(
    home: Path,
    *,
    epic_id: int,
    integration_branch: str,
    integration_worktree: str | Path,
    base_ref: str,
    slice_ids: list[int] | tuple[int, ...] = (),
    phase: EpicPhase = "decompose",
) -> EpicRunManifest:
    """Load an existing manifest or create the initial one for ``epic_id``."""

    existing = load_epic_state(home, epic_id)
    if existing is not None:
        return existing
    manifest = EpicRunManifest(
        epic_id=epic_id,
        integration_branch=integration_branch,
        integration_worktree=str(integration_worktree),
        base_ref=base_ref,
        phase=phase,
        slices=[EpicSliceRecord(id=slice_id) for slice_id in slice_ids],
        status="running",
    )
    save_epic_state(home, manifest)
    return manifest


def resume_epic_run(manifest: EpicRunManifest) -> EpicResumePoint:
    """Return the first phase/slice that still needs orchestration work.

    Merged slices are skipped so a restarted controller does not redo already
    integrated leaf work. If a manifest's current phase has advanced while a
    slice is still unmerged, the build slice is reported first because the
    integration and PR phases depend on all slices being merged.
    """

    if manifest.status == "completed":
        return EpicResumePoint(
            phase=None,
            complete=True,
            overall_status=manifest.status,
        )
    if manifest.phase == "decompose":
        return EpicResumePoint(phase="decompose", overall_status=manifest.status)

    for slice_record in manifest.slices:
        if slice_record.status != "merged":
            return EpicResumePoint(
                phase="build",
                slice_id=slice_record.id,
                slice_status=slice_record.status,
                overall_status=manifest.status,
            )

    next_phase: EpicPhase = "pr" if manifest.phase == "pr" else "integrate"
    return EpicResumePoint(phase=next_phase, overall_status=manifest.status)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None
