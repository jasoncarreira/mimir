from __future__ import annotations

import json
from pathlib import Path

import pytest

from mimir import access_control as ac


@pytest.mark.parametrize("relative", [".", "attachments/body", "unknown/file", "state/pollers/event"])
def test_home_helper_rejects_nonreference_paths(tmp_path: Path, relative: str) -> None:
    assert ac._home_reference_integrity(tmp_path, Path(relative)) == "untrusted"


@pytest.mark.parametrize("relative", ["skills", "skills/file", "docs/example/file", "skills/example/../file"])
def test_skill_reader_rejects_invalid_keys(tmp_path: Path, relative: str) -> None:
    metadata = tmp_path / ".mimir/skill-integrity.json"
    metadata.parent.mkdir()
    metadata.write_text(json.dumps({relative: "trusted"}))
    assert ac._installed_skill_integrity(tmp_path, Path(relative)) == "untrusted"


@pytest.mark.parametrize("manifest", [None, b"{broken", b"\xff", b"[]", b"null", b"{}",
    b'{"skills/example/SKILL.md":"untrusted"}', b'{"skills/example/SKILL.md":true}'])
def test_skill_reader_fails_closed(tmp_path: Path, manifest: bytes | None) -> None:
    metadata = tmp_path / ".mimir/skill-integrity.json"
    if manifest is not None:
        metadata.parent.mkdir()
        metadata.write_bytes(manifest)
    assert ac._installed_skill_integrity(tmp_path, Path("skills/example/SKILL.md")) == "untrusted"


@pytest.mark.parametrize("relative", ["skills", "skills/example/nested", "docs/example", "skills/file"])
def test_skill_recorder_rejects_invalid_root(tmp_path: Path, relative: str) -> None:
    root = tmp_path / relative
    root.parent.mkdir(parents=True, exist_ok=True)
    if relative == "skills/file":
        root.write_text("file")
    else:
        root.mkdir(exist_ok=True)
    assert ac.record_admin_installed_skill_integrity(tmp_path, root) is False
    assert not (tmp_path / ".mimir/skill-integrity.json").exists()


@pytest.mark.parametrize("manifest", [b"{broken", b"\xff", b"[]", b"null"])
def test_skill_recorder_rejects_invalid_manifest(tmp_path: Path, manifest: bytes) -> None:
    root = tmp_path / "skills/example"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text("installed")
    metadata = tmp_path / ".mimir/skill-integrity.json"
    metadata.parent.mkdir()
    metadata.write_bytes(manifest)
    assert ac.record_admin_installed_skill_integrity(tmp_path, root) is False
    assert metadata.read_bytes() == manifest


def test_skill_recorder_filters_keys_and_prunes_previous_install(tmp_path: Path) -> None:
    root = tmp_path / "skills/example"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text("installed")
    metadata = tmp_path / ".mimir/skill-integrity.json"
    metadata.parent.mkdir()
    keys = ["skills/file", "docs/other/file", "skills/other/../file", "skills//other/file",
            "skills/example/deleted", "skills/other/kept"]
    metadata.write_text(json.dumps(dict.fromkeys(keys, "trusted")))
    assert ac.record_admin_installed_skill_integrity(tmp_path, root) is True
    assert json.loads(metadata.read_text()) == {
        "skills/example/SKILL.md": "trusted", "skills/other/kept": "trusted",
    }


def test_skill_recorder_rejects_escaping_file(tmp_path: Path) -> None:
    root = tmp_path / "skills/example"
    root.mkdir(parents=True)
    outside = tmp_path / "skills/other/SKILL.md"
    outside.parent.mkdir()
    outside.write_text("not installed")
    (root / "SKILL.md").symlink_to(outside)
    assert ac.record_admin_installed_skill_integrity(tmp_path, root) is False
    assert not (tmp_path / ".mimir/skill-integrity.json").exists()
