"""Seed operator-facing docs into the agent home, and refresh them on upgrade.

`mimir setup` copies the reference docs (docs/*.md + README.md + .env.example)
into ``<home>/docs/`` so a ``pip install`` operator has them on disk — not just
on GitHub. On upgrade, docs that are still present are refreshed to the shipped
version; docs the operator deleted are NOT re-introduced (a manifest records
what was seeded, so "deleted" is distinguishable from "never seeded"). A new doc
added in a release is seeded on the next upgrade. ``--restore-docs`` force-seeds
everything regardless of prior deletion.

Only operator-facing top-level ``docs/*.md`` are seeded; ``docs/internal/``
(historical process docs) is intentionally excluded from the home.

Source resolution works in both a built wheel and a dev source tree:
- wheel: ``mimir/bundled_docs/`` (force-included at build time — see pyproject);
- source tree: the repo root (``docs/``, ``README.md``, ``.env.example``).
"""

from __future__ import annotations

import json
from importlib import metadata
from pathlib import Path

MANIFEST_REL = ".mimir/seeded_docs.json"
_PACKAGE_DIR = Path(__file__).resolve().parent


def current_version() -> str:
    for dist in ("mimir-agent", "mimir"):
        try:
            return metadata.version(dist)
        except metadata.PackageNotFoundError:
            continue
    return "unknown"


def source_root() -> Path | None:
    """Directory holding the seed layout (``docs/``, ``README.md``,
    ``.env.example``). Prefers the force-included wheel copy; falls back to the
    repo root in a dev source tree. ``None`` if neither exists."""
    bundled = _PACKAGE_DIR / "bundled_docs"
    if (bundled / "docs").is_dir():
        return bundled
    repo_root = _PACKAGE_DIR.parent
    if (repo_root / "docs").is_dir():
        return repo_root
    return None


def _seed_items(root: Path) -> list[tuple[str, Path]]:
    """``(home-relative posix path, source file)`` pairs for the seed set.

    Everything lands under ``<home>/docs/``. Only top-level ``docs/*.md`` are
    included (``docs/internal/`` and other subdirs are excluded)."""
    items: list[tuple[str, Path]] = []
    docs_dir = root / "docs"
    if docs_dir.is_dir():
        for p in sorted(docs_dir.glob("*.md")):
            items.append((f"docs/{p.name}", p))
    for name in ("README.md", ".env.example"):
        p = root / name
        if p.is_file():
            items.append((f"docs/{name}", p))
    return items


def _manifest_path(home: Path) -> Path:
    return home / MANIFEST_REL


def _read_manifest(home: Path) -> dict:
    try:
        data = json.loads(_manifest_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": None, "seeded": []}
    if not isinstance(data, dict):
        return {"version": None, "seeded": []}
    data.setdefault("version", None)
    data.setdefault("seeded", [])
    return data


def _write_manifest(home: Path, version: str | None, seeded: set[str]) -> None:
    path = _manifest_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps({"version": version, "seeded": sorted(seeded)}, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def _write_doc(home: Path, rel: str, src: Path) -> None:
    dst = home / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")


def seed_docs(home: Path, *, restore: bool = False, version: str | None = None) -> dict[str, str]:
    """Seed reference docs into ``<home>/docs/`` (called by ``mimir setup``).

    Per file: ``created`` (was never seeded → written), ``present`` (already on
    disk → left alone), ``skipped_deleted`` (seeded before, operator deleted it →
    not re-introduced). With ``restore=True`` every file is (re)written →
    ``restored``. Returns ``{home-relative-path: status}``.
    """
    root = source_root()
    if root is None:
        return {}
    version = version or current_version()
    manifest = _read_manifest(home)
    seeded: set[str] = set(manifest.get("seeded", []))
    out: dict[str, str] = {}
    for rel, src in _seed_items(root):
        dst = home / rel
        if restore:
            _write_doc(home, rel, src)
            seeded.add(rel)
            out[rel] = "restored"
        elif dst.exists():
            seeded.add(rel)
            out[rel] = "present"
        elif rel in seeded:
            out[rel] = "skipped_deleted"  # operator removed it; respect that
        else:
            _write_doc(home, rel, src)
            seeded.add(rel)
            out[rel] = "created"
    _write_manifest(home, version, seeded)
    return out


def refresh_docs(home: Path, *, version: str | None = None, force: bool = False) -> dict[str, str]:
    """Refresh seeded docs on upgrade (called at startup).

    No-op unless the running version differs from the manifest's (or ``force``).
    Per file: present → rewritten to the shipped version (``updated``/``unchanged``);
    absent + previously seeded → ``skipped_deleted`` (not re-introduced); absent +
    never seeded → ``created`` (a doc new in this release). Returns
    ``{path: status}`` (empty dict when it no-ops).
    """
    root = source_root()
    if root is None:
        return {}
    version = version or current_version()
    manifest = _read_manifest(home)
    if not force and manifest.get("version") == version:
        return {}
    seeded: set[str] = set(manifest.get("seeded", []))
    out: dict[str, str] = {}
    for rel, src in _seed_items(root):
        dst = home / rel
        if dst.exists():
            new = src.read_text(encoding="utf-8")
            if dst.read_text(encoding="utf-8") == new:
                out[rel] = "unchanged"
            else:
                dst.write_text(new, encoding="utf-8")
                out[rel] = "updated"
            seeded.add(rel)
        elif rel in seeded:
            out[rel] = "skipped_deleted"
        else:
            _write_doc(home, rel, src)
            seeded.add(rel)
            out[rel] = "created"
    _write_manifest(home, version, seeded)
    return out
