"""Tests for docs seeding + upgrade refresh (mimir/doc_seed.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mimir import doc_seed


def _make_source(tmp_path: Path) -> Path:
    root = tmp_path / "src"
    (root / "docs" / "internal").mkdir(parents=True)
    (root / "docs" / "configuration.md").write_text("config v1\n", encoding="utf-8")
    (root / "docs" / "web-ui-auth.md").write_text("auth v1\n", encoding="utf-8")
    (root / "docs" / "internal" / "FUTURE_WORK.md").write_text("internal\n", encoding="utf-8")
    (root / "README.md").write_text("readme v1\n", encoding="utf-8")
    (root / ".env.example").write_text("env v1\n", encoding="utf-8")
    return root


@pytest.fixture
def source(tmp_path, monkeypatch):
    root = _make_source(tmp_path)
    monkeypatch.setattr(doc_seed, "source_root", lambda: root)
    return root


def _home(tmp_path) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    return h


def test_seed_creates_operator_docs_and_excludes_internal(source, tmp_path):
    home = _home(tmp_path)
    out = doc_seed.seed_docs(home, version="1.0")
    assert out == {
        "docs/configuration.md": "created",
        "docs/web-ui-auth.md": "created",
        "docs/README.md": "created",
        "docs/.env.example": "created",
    }
    assert (home / "docs" / "configuration.md").read_text() == "config v1\n"
    assert (home / "docs" / "README.md").exists()
    # docs/internal/ is NOT seeded into the home
    assert not (home / "docs" / "internal").exists()


def test_seed_leaves_present_files_alone(source, tmp_path):
    home = _home(tmp_path)
    doc_seed.seed_docs(home, version="1.0")
    (home / "docs" / "configuration.md").write_text("OPERATOR EDIT\n", encoding="utf-8")
    out = doc_seed.seed_docs(home, version="1.0")
    assert out["docs/configuration.md"] == "present"
    assert (home / "docs" / "configuration.md").read_text() == "OPERATOR EDIT\n"


def test_seed_does_not_reintroduce_deleted(source, tmp_path):
    home = _home(tmp_path)
    doc_seed.seed_docs(home, version="1.0")
    (home / "docs" / "web-ui-auth.md").unlink()
    out = doc_seed.seed_docs(home, version="1.0")
    assert out["docs/web-ui-auth.md"] == "skipped_deleted"
    assert not (home / "docs" / "web-ui-auth.md").exists()


def test_restore_forces_all_including_deleted(source, tmp_path):
    home = _home(tmp_path)
    doc_seed.seed_docs(home, version="1.0")
    (home / "docs" / "web-ui-auth.md").unlink()
    (home / "docs" / "configuration.md").write_text("OPERATOR EDIT\n", encoding="utf-8")
    out = doc_seed.seed_docs(home, version="1.0", restore=True)
    assert set(out.values()) == {"restored"}
    assert (home / "docs" / "web-ui-auth.md").read_text() == "auth v1\n"      # recreated
    assert (home / "docs" / "configuration.md").read_text() == "config v1\n"  # overwritten


def test_refresh_updates_present_on_version_change(source, tmp_path):
    home = _home(tmp_path)
    doc_seed.seed_docs(home, version="1.0")
    (source / "docs" / "configuration.md").write_text("config v2\n", encoding="utf-8")
    out = doc_seed.refresh_docs(home, version="2.0")
    assert out["docs/configuration.md"] == "updated"
    assert out["docs/web-ui-auth.md"] == "unchanged"
    assert (home / "docs" / "configuration.md").read_text() == "config v2\n"


def test_refresh_skips_deleted_docs(source, tmp_path):
    home = _home(tmp_path)
    doc_seed.seed_docs(home, version="1.0")
    (home / "docs" / "web-ui-auth.md").unlink()
    out = doc_seed.refresh_docs(home, version="2.0")
    assert out["docs/web-ui-auth.md"] == "skipped_deleted"
    assert not (home / "docs" / "web-ui-auth.md").exists()


def test_refresh_seeds_doc_new_in_release(source, tmp_path):
    home = _home(tmp_path)
    doc_seed.seed_docs(home, version="1.0")
    (source / "docs" / "brand-new.md").write_text("new\n", encoding="utf-8")
    out = doc_seed.refresh_docs(home, version="2.0")
    assert out["docs/brand-new.md"] == "created"
    assert (home / "docs" / "brand-new.md").read_text() == "new\n"


def test_refresh_noop_when_version_unchanged(source, tmp_path):
    home = _home(tmp_path)
    doc_seed.seed_docs(home, version="1.0")
    (source / "docs" / "configuration.md").write_text("config v2\n", encoding="utf-8")
    assert doc_seed.refresh_docs(home, version="1.0") == {}
    # unchanged on disk because refresh no-opped
    assert (home / "docs" / "configuration.md").read_text() == "config v1\n"


def test_graceful_when_no_source(tmp_path, monkeypatch):
    monkeypatch.setattr(doc_seed, "source_root", lambda: None)
    home = _home(tmp_path)
    assert doc_seed.seed_docs(home, version="1.0") == {}
    assert doc_seed.refresh_docs(home, version="1.0") == {}
