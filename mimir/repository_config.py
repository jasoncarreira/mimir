"""Neutral repository inventory shared by authorization and repository consumers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class RepositoryTestSuite:
    name: str
    command: str
    default: bool = False
    selector_prefixes: tuple[str, ...] = ()
    selector_suffixes: tuple[str, ...] = ()


@dataclass(frozen=True)
class RepositoryConfig:
    slug: str
    root: Path
    mode: str
    origin: str
    base_branch: str
    test_command: str | None = None
    test_suites: tuple[RepositoryTestSuite, ...] = ()


@dataclass(frozen=True)
class AllowedRootConfig:
    root: Path
    mode: str


@dataclass(frozen=True)
class RepositoryInventory:
    repositories: tuple[RepositoryConfig, ...] = ()
    allowed_roots: tuple[AllowedRootConfig, ...] = ()
    declared: bool = False

    @classmethod
    def load(cls, path: Path) -> "RepositoryInventory":
        if not path.exists():
            return cls()
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError("repository inventory root must be a mapping")
        repositories = _parse_repositories(data.get("repositories") or ())
        allowed_roots = _parse_allowed_roots(data.get("allowed_roots") or ())
        overlap = {repo.root for repo in repositories}.intersection(
            root.root for root in allowed_roots
        )
        if overlap:
            paths = ", ".join(str(path) for path in sorted(overlap))
            raise ValueError(f"repository roots cannot also be allowed_roots: {paths}")
        return cls(
            repositories=repositories,
            allowed_roots=allowed_roots,
            declared=("repositories" in data or "allowed_roots" in data),
        )

    def repository(self, slug: str) -> RepositoryConfig | None:
        normalized = slug.strip().lower()
        return next((repo for repo in self.repositories if repo.slug == normalized), None)

    def repository_for_root(self, root: Path) -> RepositoryConfig | None:
        resolved = root.resolve()
        return next((repo for repo in self.repositories if repo.root == resolved), None)


_REPOSITORY_SLUG = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_TEST_SUITE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_GITHUB_ORIGIN = re.compile(
    r"(?:https?://github\.com/|ssh://git@github\.com/|git@github\.com:)"
    r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?"
)


def valid_repository_slug(value: object) -> bool:
    return isinstance(value, str) and _REPOSITORY_SLUG.fullmatch(value) is not None


def _parse_mode(value: Any, label: str) -> str:
    if value not in {"ro", "rw"}:
        raise ValueError(f"{label}.mode must be 'ro' or 'rw'")
    return str(value)


def _parse_absolute_root(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value or "~" in value:
        raise ValueError(f"{label}.root must be an absolute path")
    root = Path(value)
    if not root.is_absolute() or ".." in root.parts:
        raise ValueError(f"{label}.root must be an absolute path without ~ or ..")
    return root.resolve()


def _parse_test_suites(
    value: Any, label: str, test_command: str | None,
) -> tuple[RepositoryTestSuite, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    suites: list[RepositoryTestSuite] = []
    names: set[str] = set()
    has_default = False
    for index, item in enumerate(value):
        suite_label = f"{label}[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{suite_label} must be a mapping")
        missing = [key for key in ("name", "command") if key not in item]
        if missing:
            raise ValueError(f"{suite_label} missing required field(s): {', '.join(missing)}")
        if set(item) - {"name", "command", "default", "selector_prefixes", "selector_suffixes"}:
            raise ValueError(f"{suite_label} contains unknown fields")
        name = item["name"]
        if not isinstance(name, str) or _TEST_SUITE_NAME.fullmatch(name) is None:
            raise ValueError(f"{suite_label}.name must be a safe name matching {_TEST_SUITE_NAME.pattern}")
        if name in names:
            raise ValueError(f"{suite_label} duplicate test suite name: {name}")
        if name == "default" and test_command is not None:
            raise ValueError(f"{suite_label}.name 'default' is reserved when test_command is nonnull")
        command = item["command"]
        if not isinstance(command, str) or not command.strip():
            raise ValueError(f"{suite_label}.command must be a non-empty string")
        default = item.get("default", False)
        if not isinstance(default, bool):
            raise ValueError(f"{suite_label}.default must be a boolean")
        if default and has_default:
            raise ValueError(f"{label} may have at most one default suite")
        selectors: dict[str, tuple[str, ...]] = {}
        for field in ("selector_prefixes", "selector_suffixes"):
            entries = item.get(field, [])
            if not isinstance(entries, list) or any(
                not isinstance(entry, str) or not entry.strip() for entry in entries
            ):
                raise ValueError(f"{suite_label}.{field} must be a list of non-empty strings")
            selectors[field] = tuple(entries)
        names.add(name)
        has_default = has_default or default
        suites.append(RepositoryTestSuite(name, command, default, **selectors))
    return tuple(suites)


def _parse_repositories(value: Any) -> tuple[RepositoryConfig, ...]:
    if not isinstance(value, list | tuple):
        raise ValueError("repository inventory repositories must be a list")
    repositories: list[RepositoryConfig] = []
    slugs: set[str] = set()
    roots: set[Path] = set()
    for index, item in enumerate(value):
        label = f"repository inventory repositories[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{label} must be a mapping")
        missing = [
            key
            for key in ("slug", "root", "mode", "origin", "base_branch")
            if key not in item
        ]
        if missing:
            raise ValueError(f"{label} missing required field(s): {', '.join(missing)}")
        slug = item["slug"]
        if not valid_repository_slug(slug):
            raise ValueError(f"{label}.slug must be owner/repository")
        slug = slug.lower()
        root = _parse_absolute_root(item["root"], label)
        origin = item["origin"]
        origin_match = _GITHUB_ORIGIN.fullmatch(origin) if isinstance(origin, str) else None
        if origin_match is None:
            raise ValueError(f"{label}.origin must be a GitHub repository URL")
        origin_slug = f"{origin_match.group('owner')}/{origin_match.group('repo')}".lower()
        if origin_slug != slug:
            raise ValueError(
                f"{label}.origin names {origin_slug}, which disagrees with slug {slug}"
            )
        base_branch = item["base_branch"]
        if not isinstance(base_branch, str) or not base_branch.strip():
            raise ValueError(f"{label}.base_branch must be a non-empty string")
        test_command = item.get("test_command")
        if test_command is not None and not isinstance(test_command, str):
            raise ValueError(f"{label}.test_command must be a string or null")
        test_suites = _parse_test_suites(
            item.get("test_suites", []), f"{label}.test_suites", test_command,
        )
        if set(item) - {"slug", "root", "mode", "origin", "base_branch", "test_command", "test_suites"}:
            raise ValueError(f"{label} contains unknown fields")
        if slug in slugs:
            raise ValueError(f"duplicate repository slug: {slug}")
        if root in roots:
            raise ValueError(f"duplicate repository root: {root}")
        slugs.add(slug)
        roots.add(root)
        repositories.append(
            RepositoryConfig(
                slug=slug,
                root=root,
                mode=_parse_mode(item["mode"], label),
                origin=origin.strip(),
                base_branch=base_branch.strip(),
                test_command=test_command,
                test_suites=test_suites,
            )
        )
    return tuple(repositories)


def _parse_allowed_roots(value: Any) -> tuple[AllowedRootConfig, ...]:
    if not isinstance(value, list | tuple):
        raise ValueError("repository inventory allowed_roots must be a list")
    roots: set[Path] = set()
    parsed: list[AllowedRootConfig] = []
    for index, item in enumerate(value):
        label = f"repository inventory allowed_roots[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{label} must be a mapping")
        if set(item) != {"root", "mode"}:
            raise ValueError(f"{label} must contain exactly root and mode")
        root = _parse_absolute_root(item["root"], label)
        if root in roots:
            raise ValueError(f"duplicate allowed root: {root}")
        roots.add(root)
        parsed.append(AllowedRootConfig(root, _parse_mode(item["mode"], label)))
    return tuple(parsed)
