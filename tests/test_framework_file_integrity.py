from __future__ import annotations

import json
from pathlib import Path

import pytest

from mimir import access_control


EPOCH = "__ledger_epoch_ns__"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("MIMIR_HOME", str(root))
    monkeypatch.delenv("MIMIR_FILE_TOOL_ROOTS", raising=False)
    return root.resolve()


def ledger(home: Path) -> dict:
    return json.loads((home / ".mimir/file-integrity.json").read_text())


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
        access_control.record_framework_file_integrity(
            home, {home / relative: b"framework"}, publish,
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


@pytest.mark.parametrize(
    "raw",
    [
        "{broken",
        "[]",
        "null",
        "42",
        *[json.dumps({EPOCH: value}) for value in (None, "1", 1.5, True, False, 0, -1)],
    ],
    ids=["invalid-json", "list", "null", "scalar", "null-epoch", "string-epoch",
         "float-epoch", "true-epoch", "false-epoch", "zero-epoch", "negative-epoch"],
)
def test_malformed_ledger_rejected_without_publication(home: Path, raw: str) -> None:
    metadata = home / ".mimir/file-integrity.json"
    metadata.parent.mkdir()
    metadata.write_text(raw)

    def publish() -> None:
        pytest.fail("malformed ledger reached publication")

    with pytest.raises(ValueError):
        access_control.record_framework_file_integrity(
            home, {home / "prompts/output.txt": b"new"}, publish,
        )

    assert metadata.read_text() == raw


@pytest.mark.parametrize("change", ["content", "symlink", "parent-symlink", "missing"])
def test_changed_publication_leaves_entire_batch_untrusted(home: Path, change: str) -> None:
    first = home / "prompts/first.txt"
    second = home / "docs/second.txt"
    for path in (first, second):
        access_control.write_framework_file(home, path, b"expected")
    assert ledger(home)["prompts/first.txt"] == "trusted"
    assert ledger(home)["docs/second.txt"] == "trusted"

    def publish() -> None:
        assert ledger(home)["prompts/first.txt"] == "untrusted"
        assert ledger(home)["docs/second.txt"] == "untrusted"
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
        access_control.record_framework_file_integrity(
            home, {first: b"expected", second: b"expected"}, publish,
        )

    assert ledger(home)["prompts/first.txt"] == "untrusted"
    assert ledger(home)["docs/second.txt"] == "untrusted"
    assert access_control._filesystem_result_integrity(None, str(first)) == (
        "untrusted", "active_ingest",
    )


def test_publish_failure_invalidates_previously_trusted_output(home: Path) -> None:
    destination = home / "prompts/output.txt"
    access_control.write_framework_file(home, destination, b"old")
    assert ledger(home)["prompts/output.txt"] == "trusted"

    def publish() -> None:
        assert ledger(home)["prompts/output.txt"] == "untrusted"
        raise OSError("publication failed")

    with pytest.raises(OSError, match="publication failed"):
        access_control.record_framework_file_integrity(home, {destination: b"new"}, publish)

    assert destination.read_bytes() == b"old"
    assert ledger(home)["prompts/output.txt"] == "untrusted"
    assert access_control._filesystem_result_integrity(None, str(destination)) == (
        "untrusted", "active_ingest",
    )


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
    assert ledger(home)["prompts/output.txt"] == "untrusted"


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
    payload = ledger(home)
    assert payload[relative] == "trusted"
    assert type(payload[EPOCH]) is int and payload[EPOCH] > 0
    assert access_control._filesystem_result_integrity(None, str(destination)) == (
        "trusted", "informational",
    )


@pytest.mark.parametrize("prune", [False, True])
@pytest.mark.parametrize("epoch", [None, 1])
def test_successful_batch_preserves_ledger_and_prunes_only_missing_builtins(
    home: Path, prune: bool, epoch: int | None,
) -> None:
    metadata = home / ".mimir/file-integrity.json"
    metadata.parent.mkdir()
    existing = home / ".mimir_builtin_skills/existing/SKILL.md"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"existing")
    original = {
        ".mimir_builtin_skills/missing/SKILL.md": "trusted",
        ".mimir_builtin_skills/existing/SKILL.md": "untrusted",
        ".mimir_builtin_skills-other/missing": "trusted",
        "docs/missing.txt": "untrusted",
        "prompts/old.txt": "trusted",
        "prompts/tainted.txt": "untrusted",
    }
    if epoch is not None:
        original[EPOCH] = epoch
    metadata.write_text(json.dumps(original))
    files = {home / f"prompts/{name}.txt": name.encode() for name in ("old", "tainted", "new")}

    def publish() -> None:
        for path, content in files.items():
            assert ledger(home)[path.relative_to(home).as_posix()] == "untrusted"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

    assert access_control.record_framework_file_integrity(
        home, files, publish, prune_builtin=prune,
    ) == 2
    payload = ledger(home)
    for key, value in original.items():
        if key in ("prompts/old.txt", "prompts/tainted.txt"):
            continue
        if prune and key == ".mimir_builtin_skills/missing/SKILL.md":
            assert key not in payload
        else:
            assert payload[key] == value
    assert type(payload[EPOCH]) is int and payload[EPOCH] > 0
    for path, content in files.items():
        assert path.read_bytes() == content
        assert payload[path.relative_to(home).as_posix()] == "trusted"
        assert access_control._filesystem_result_integrity(None, str(path)) == (
            "trusted", "informational",
        )
