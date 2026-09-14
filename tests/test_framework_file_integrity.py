from __future__ import annotations

from pathlib import Path

import pytest

from mimir import access_control


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("MIMIR_HOME", str(root))
    monkeypatch.delenv("MIMIR_FILE_TOOL_ROOTS", raising=False)
    return root.resolve()


@pytest.mark.parametrize(
    "relative",
    [
        "skills/example/SKILL.md",
        "state/pollers/cursor.json",
        "state/pollers",
        "attachments/output.txt",
        "prompts",
        ".",
        "../home-other/prompts/output.txt",
    ],
)
def test_rejects_excluded_destinations_before_publish(home: Path, relative: str) -> None:
    def publish() -> None:
        pytest.fail("invalid destination reached publication")

    with pytest.raises(ValueError):
        access_control.publish_framework_files(
            home, {home / "docs/valid.txt": b"valid", home / relative: b"framework"}, publish,
        )
    assert not (home / ".mimir/file-integrity.json").exists()


def test_rejects_destination_symlink_escaping_home(home: Path) -> None:
    outside = home.parent / "outside"
    outside.mkdir()
    (home / "prompts").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError):
        access_control.write_framework_file(home, home / "prompts/output.txt", b"new")

    assert list(outside.iterdir()) == []
    assert not (home / ".mimir/file-integrity.json").exists()


def test_publication_ignores_malformed_legacy_ledger(home: Path) -> None:
    metadata = home / ".mimir/file-integrity.json"
    metadata.parent.mkdir()
    metadata.write_text("{broken")

    destination = home / "prompts/output.txt"
    access_control.write_framework_file(home, destination, b"new")
    assert destination.read_bytes() == b"new"
    assert metadata.read_text() == "{broken"


@pytest.mark.parametrize("change", ["content", "symlink", "parent-symlink", "missing"])
def test_changed_publication_fails_verification(home: Path, change: str) -> None:
    first = home / "prompts/first.txt"
    second = home / "docs/second.txt"
    for path in (first, second):
        access_control.write_framework_file(home, path, b"expected")

    def publish() -> None:
        if change == "content":
            second.write_bytes(b"tampered")
        elif change == "symlink":
            target = home / "docs/target.txt"
            target.write_bytes(b"expected")
            second.unlink()
            second.symlink_to(target)
        elif change == "parent-symlink":
            target = home / "relocated-docs"
            second.parent.rename(target)
            second.parent.symlink_to(target, target_is_directory=True)
        else:
            second.unlink()

    error = FileNotFoundError if change == "missing" else ValueError
    with pytest.raises(error):
        access_control.publish_framework_files(
            home, {first: b"expected", second: b"expected"}, publish,
        )

    assert first.read_bytes() == b"expected"
    assert not (home / ".mimir/file-integrity.json").exists()


def test_publish_failure_propagates_without_changing_output(home: Path) -> None:
    destination = home / "prompts/output.txt"
    access_control.write_framework_file(home, destination, b"old")

    def publish() -> None:
        raise OSError("publication failed")

    with pytest.raises(OSError, match="publication failed"):
        access_control.publish_framework_files(home, {destination: b"new"}, publish)

    assert destination.read_bytes() == b"old"


@pytest.mark.parametrize("obstacle", ["file", "symlink"])
def test_writer_refuses_preplanted_temporary_path(home: Path, obstacle: str) -> None:
    destination = home / "prompts/output.txt"
    access_control.write_framework_file(home, destination, b"old")
    temporary = destination.with_name(destination.name + ".tmp")
    victim = home / "victim.txt"
    victim.write_bytes(b"untouched")
    if obstacle == "symlink":
        temporary.symlink_to(victim)
    else:
        temporary.write_bytes(b"untouched")

    with pytest.raises(FileExistsError):
        access_control.write_framework_file(home, destination, b"new")

    assert victim.read_bytes() == temporary.read_bytes() == b"untouched"
    assert destination.read_bytes() == b"old"


@pytest.mark.parametrize(
    "relative",
    [".mimir_builtin_skills/example/SKILL.md", "docs/output.txt", "memory/output.txt",
     "prompts/output.txt", "state/output.txt", "state/pollers-other/output.txt"],
)
def test_successful_writer_outputs_are_trusted(home: Path, relative: str) -> None:
    destination = home / relative
    content = b"framework\x00\xff\n"

    access_control.write_framework_file(home, destination, content)

    assert destination.read_bytes() == content
    assert not destination.with_name(destination.name + ".tmp").exists()
    assert not (home / ".mimir/file-integrity.json").exists()
    assert access_control._filesystem_result_integrity(None, str(destination)) == (
        "trusted", "informational",
    )


def test_successful_batch_publishes_all_files_without_ledger(home: Path) -> None:
    files = {home / f"prompts/{name}.txt": name.encode() for name in ("old", "tainted", "new")}

    def publish() -> None:
        for path, content in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

    access_control.publish_framework_files(home, files, publish)
    assert not (home / ".mimir/file-integrity.json").exists()
    for path, content in files.items():
        assert path.read_bytes() == content
        assert access_control._filesystem_result_integrity(None, str(path)) == (
            "trusted", "informational",
        )
