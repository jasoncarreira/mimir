from __future__ import annotations

import os
from dataclasses import FrozenInstanceError
from pathlib import Path
import subprocess

import pytest
import yaml

from mimir import access_control
from mimir.config import Config
from mimir.repository_config import RepositoryConfig, RepositoryInventory, RepositoryTestSuite
from mimir.worklink.backends.registry import WorklinkConfig


def _git_checkout(path: Path, origin: str) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "remote", "add", "origin", origin],
        check=True,
    )


def _write_inventory(
    home: Path,
    checkout: Path,
    allowed: Path,
    *,
    origin: str = "https://github.com/owner/repo.git",
) -> None:
    home.mkdir()
    (home / "repositories.yaml").write_text(
        f"""
repositories:
  - slug: owner/repo
    root: {checkout}
    mode: rw
    origin: {origin}
    base_branch: trunk
    test_command: /usr/bin/true --repository
allowed_roots:
  - root: {allowed}
    mode: ro
""".strip(),
        encoding="utf-8",
    )
    (home / "worklink.yaml").write_text(
        "repository: owner/repo\n",
        encoding="utf-8",
    )


def _clear_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "GITHUB_REPOS",
        "MIMIR_FILE_TOOL_ROOTS",
        "WORKLINK_REPO",
        "MIMIR_WORKLINK_REPO",
    ):
        monkeypatch.delenv(name, raising=False)


def test_repository_record_drives_binding_roots_and_legacy_projections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    checkout = tmp_path / "checkout"
    allowed = tmp_path / "reference"
    allowed.mkdir()
    _git_checkout(checkout, "https://github.com/owner/repo.git")
    _write_inventory(home, checkout, allowed)
    _clear_legacy(monkeypatch)
    monkeypatch.setenv("MIMIR_HOME", str(home))

    config = Config.from_env()
    record = RepositoryInventory.load(home / "repositories.yaml").repository("OWNER/REPO")

    assert record is not None
    assert record.base_branch == "trunk"
    assert record.test_command == "/usr/bin/true --repository"
    assert access_control._canonical_repo_binding("owner/repo") == (
        str(checkout.resolve()),
        "https://github.com/owner/repo.git",
    )
    assert access_control._configured_repo_write_roots() == [checkout.resolve()]
    assert access_control._configured_repo_roots() == [checkout.resolve()]
    assert (str(allowed.resolve()), "ro") in config.file_tool_roots
    assert allowed.resolve() not in access_control._configured_repo_roots()
    assert os.environ["GITHUB_REPOS"] == "owner/repo"
    assert os.environ["WORKLINK_REPO"] == str(checkout.resolve())


@pytest.mark.parametrize(
    "empty_name",
    ["GITHUB_REPOS", "MIMIR_FILE_TOOL_ROOTS", "WORKLINK_REPO"],
)
def test_empty_legacy_repository_value_uses_declarative_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, empty_name: str,
) -> None:
    home = tmp_path / "home"
    checkout = tmp_path / "checkout"
    allowed = tmp_path / "reference"
    allowed.mkdir()
    _git_checkout(checkout, "https://github.com/owner/repo.git")
    _write_inventory(home, checkout, allowed)
    _clear_legacy(monkeypatch)
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv(empty_name, "")

    config = Config.from_env()

    assert os.environ["GITHUB_REPOS"] == "owner/repo"
    assert dict(config.file_tool_roots)[str(checkout.resolve())] == "rw"
    assert os.environ["WORKLINK_REPO"] == str(checkout.resolve())


def test_worklink_target_must_name_declared_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    checkout = tmp_path / "checkout"
    allowed = tmp_path / "reference"
    allowed.mkdir()
    _git_checkout(checkout, "https://github.com/owner/repo.git")
    _write_inventory(home, checkout, allowed)
    (home / "worklink.yaml").write_text(
        "repository: owner/missing\n",
        encoding="utf-8",
    )
    _clear_legacy(monkeypatch)
    monkeypatch.setenv("MIMIR_HOME", str(home))

    with pytest.raises(
        RuntimeError,
        match="worklink.yaml repository does not name a declared repository: owner/missing",
    ):
        Config.from_env()


def test_repository_origin_mismatch_is_fatal_and_names_both_origins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    checkout = tmp_path / "checkout"
    allowed = tmp_path / "reference"
    allowed.mkdir()
    _git_checkout(checkout, "git@github.com:owner/repo.git")
    _write_inventory(home, checkout, allowed)
    _clear_legacy(monkeypatch)
    monkeypatch.setenv("MIMIR_HOME", str(home))

    with pytest.raises(RuntimeError) as exc_info:
        Config.from_env()

    message = str(exc_info.value)
    assert "repository owner/repo did not bind" in message
    assert "https://github.com/owner/repo.git" in message
    assert "git@github.com:owner/repo.git" in message


def test_parent_directory_cannot_satisfy_repository_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    parent = tmp_path / "checkouts"
    parent.mkdir()
    checkout = parent / "repo"
    allowed = tmp_path / "reference"
    allowed.mkdir()
    _git_checkout(checkout, "https://github.com/owner/repo.git")
    _write_inventory(home, parent, allowed)
    _clear_legacy(monkeypatch)
    monkeypatch.setenv("MIMIR_HOME", str(home))

    with pytest.raises(RuntimeError, match="found 'not a git checkout'"):
        Config.from_env()


def test_legacy_root_disagreement_fails_closed_with_both_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    checkout = tmp_path / "checkout"
    allowed = tmp_path / "reference"
    other = tmp_path / "other"
    allowed.mkdir()
    other.mkdir()
    _git_checkout(checkout, "https://github.com/owner/repo.git")
    _write_inventory(home, checkout, allowed)
    _clear_legacy(monkeypatch)
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv("MIMIR_FILE_TOOL_ROOTS", f"{checkout}:rw,{other}:ro")

    with pytest.raises(RuntimeError) as exc_info:
        Config.from_env()

    message = str(exc_info.value)
    assert "MIMIR_FILE_TOOL_ROOTS disagrees" in message
    assert str(other) in message
    assert str(allowed) in message


@pytest.mark.parametrize(
    ("name", "legacy_value", "declared_value"),
    [
        ("GITHUB_REPOS", "other/repo", "owner/repo"),
        ("WORKLINK_REPO", "{other}", "{checkout}"),
    ],
)
def test_nonempty_legacy_disagreement_fails_closed_with_both_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    legacy_value: str,
    declared_value: str,
) -> None:
    home = tmp_path / "home"
    checkout = tmp_path / "checkout"
    allowed = tmp_path / "reference"
    other = tmp_path / "other"
    allowed.mkdir()
    other.mkdir()
    _git_checkout(checkout, "https://github.com/owner/repo.git")
    _write_inventory(home, checkout, allowed)
    _clear_legacy(monkeypatch)
    monkeypatch.setenv("MIMIR_HOME", str(home))
    legacy_value = legacy_value.format(other=other, checkout=checkout)
    declared_value = declared_value.format(other=other, checkout=checkout)
    monkeypatch.setenv(name, legacy_value)

    with pytest.raises(RuntimeError) as exc_info:
        Config.from_env()

    message = str(exc_info.value)
    assert name in message
    assert legacy_value in message
    assert declared_value in message


def test_worklink_config_names_one_neutral_repository(tmp_path: Path) -> None:
    config = tmp_path / "worklink.yaml"
    config.write_text("repository: OWNER/Service\n", encoding="utf-8")

    assert WorklinkConfig.load(config).repository == "owner/service"


def test_repository_inventory_rejects_worklink_back_reference(tmp_path: Path) -> None:
    config = tmp_path / "repositories.yaml"
    config.write_text(
        f"""
repositories:
  - slug: owner/repo
    root: {tmp_path / 'repo'}
    mode: rw
    origin: https://github.com/owner/repo.git
    base_branch: main
    worklink: true
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="contains unknown fields"):
        RepositoryInventory.load(config)


def test_duplicate_repository_root_is_rejected(tmp_path: Path) -> None:
    config = tmp_path / "repositories.yaml"
    config.write_text(
        f"""
repositories:
  - slug: owner/one
    root: {tmp_path / 'repo'}
    mode: rw
    origin: https://github.com/owner/one.git
    base_branch: main
  - slug: owner/two
    root: {tmp_path / 'repo'}
    mode: rw
    origin: https://github.com/owner/two.git
    base_branch: main
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate repository root"):
        RepositoryInventory.load(config)


def _load_test_suite_inventory(tmp_path: Path, **fields: object) -> RepositoryConfig:
    config = tmp_path / "repositories.yaml"
    config.write_text(
        yaml.safe_dump({"repositories": [{
            "slug": "owner/repo",
            "root": str(tmp_path / "repo"),
            "mode": "rw",
            "origin": "https://github.com/owner/repo.git",
            "base_branch": "main",
            **fields,
        }]}),
        encoding="utf-8",
    )
    return RepositoryInventory.load(config).repositories[0]


def test_repository_test_suite_defaults_and_immutability(tmp_path: Path) -> None:
    suite = RepositoryTestSuite("unit", "pytest")
    assert suite.default is False
    assert suite.selector_prefixes == suite.selector_suffixes == ()
    with pytest.raises(FrozenInstanceError):
        suite.name = "changed"
    repo = RepositoryConfig("owner/repo", tmp_path, "rw", "origin", "main")
    assert repo.test_suites == ()


@pytest.mark.parametrize("test_command", [None, "pytest", ""])
@pytest.mark.parametrize("explicit_default", [False, True])
def test_repository_test_suites_parse(tmp_path: Path, test_command, explicit_default) -> None:
    command = " uv run pytest -q "
    repo = _load_test_suite_inventory(
        tmp_path,
        test_command=test_command,
        test_suites=[
            {"name": "Unit_3.11-fast", "command": command, "default": explicit_default,
             "selector_prefixes": ["tests/", "src/"], "selector_suffixes": [".py", "_test.py"]},
            {"name": "integration", "command": "make integration"},
        ],
    )
    assert repo.test_command == test_command
    assert repo.test_suites == (
        RepositoryTestSuite("Unit_3.11-fast", command, explicit_default,
                            ("tests/", "src/"), (".py", "_test.py")),
        RepositoryTestSuite("integration", "make integration"),
    )


@pytest.mark.parametrize("fields", [{}, {"test_suites": []}, {"test_command": "pytest"}])
def test_repository_test_suites_empty_without_synthesis(tmp_path: Path, fields) -> None:
    assert _load_test_suite_inventory(tmp_path, **fields).test_suites == ()


@pytest.mark.parametrize("default", [False, True])
def test_default_suite_name_allowed_without_legacy_command(tmp_path: Path, default) -> None:
    repo = _load_test_suite_inventory(
        tmp_path, test_command=None,
        test_suites=[{"name": "default", "command": "pytest", "default": default,
                      "selector_prefixes": [], "selector_suffixes": []}],
    )
    assert repo.test_suites == (RepositoryTestSuite("default", "pytest", default),)


@pytest.mark.parametrize("test_command", ["pytest", ""])
@pytest.mark.parametrize("default", [False, True])
def test_default_suite_name_reserved_with_legacy_command(tmp_path: Path, test_command, default) -> None:
    with pytest.raises(ValueError, match="reserved"):
        _load_test_suite_inventory(
            tmp_path, test_command=test_command,
            test_suites=[{"name": "default", "command": "pytest", "default": default}],
        )


@pytest.mark.parametrize("suites, message", [
    (None, "must be a list"),
    ({}, "must be a list"),
    ("unit", "must be a list"),
    (False, "must be a list"),
    (0, "must be a list"),
    ([None], "must be a mapping"),
    (["unit"], "must be a mapping"),
    ([[]], "must be a mapping"),
    ([{}], "missing required field"),
    ([{"name": "unit"}], "missing required field.*command"),
    ([{"command": "pytest"}], "missing required field.*name"),
    ([{"name": "unit", "command": "pytest", "typo": True}], "unknown fields"),
    ([{"name": "unit", "command": "pytest"}] * 2, "duplicate test suite name"),
    ([{"name": "unit", "command": "pytest", "default": True},
      {"name": "integration", "command": "make test", "default": True}], "at most one default"),
])
def test_repository_test_suites_reject_invalid_structure(tmp_path: Path, suites, message) -> None:
    with pytest.raises(ValueError, match=message):
        _load_test_suite_inventory(tmp_path, test_suites=suites)


@pytest.mark.parametrize("name", [None, False, 1, [], {}, "", " ", "unit tests",
                                      "../unit", "a/b", "a\\b", "-unit", ".", "..",
                                      "unit;pwd", "unit\n", "unit$HOME"])
def test_repository_test_suites_reject_unsafe_names(tmp_path: Path, name) -> None:
    with pytest.raises(ValueError, match="name must be a safe name"):
        _load_test_suite_inventory(tmp_path, test_suites=[{"name": name, "command": "pytest"}])


@pytest.mark.parametrize("command", [None, False, 1, [], {}, "", " \n\t"])
def test_repository_test_suites_reject_invalid_commands(tmp_path: Path, command) -> None:
    with pytest.raises(ValueError, match="command must be a non-empty string"):
        _load_test_suite_inventory(tmp_path, test_suites=[{"name": "unit", "command": command}])


@pytest.mark.parametrize("default", [None, 0, 1, "true", "false", [], {}])
def test_repository_test_suites_reject_non_boolean_default(tmp_path: Path, default) -> None:
    with pytest.raises(ValueError, match="default must be a boolean"):
        _load_test_suite_inventory(
            tmp_path, test_suites=[{"name": "unit", "command": "pytest", "default": default}],
        )


@pytest.mark.parametrize("field", ["selector_prefixes", "selector_suffixes"])
@pytest.mark.parametrize("value", [None, "tests/", {}, False, 1, [None], [False], [1],
                                   [[]], [{}], [""], [" \t"], ["valid", ""]])
def test_repository_test_suites_reject_invalid_selectors(tmp_path: Path, field, value) -> None:
    with pytest.raises(ValueError, match=field + " must be a list of non-empty strings"):
        _load_test_suite_inventory(
            tmp_path, test_suites=[{"name": "unit", "command": "pytest", field: value}],
        )
