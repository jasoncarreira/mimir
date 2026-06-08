"""Skill-declared required extras + start.sh resolver (chainlink #406)."""

from __future__ import annotations

import argparse
import tomllib
from pathlib import Path

from mimir.skill_install import cmd_required_extras, required_extras
from mimir.skill_md import frontmatter_yaml


def _write_skill(skills_dir: Path, name: str, frontmatter: str) -> None:
    d = skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(frontmatter, encoding="utf-8")


# ── frontmatter_yaml ─────────────────────────────────────────────────


def test_frontmatter_yaml_inline_list():
    fm = frontmatter_yaml("---\nname: x\nrequires_extras: [gepa, foo]\n---\nbody\n")
    assert fm["name"] == "x"
    assert fm["requires_extras"] == ["gepa", "foo"]


def test_frontmatter_yaml_block_list():
    text = "---\nname: x\nrequires_extras:\n  - gepa\n  - foo\n---\nbody\n"
    assert frontmatter_yaml(text)["requires_extras"] == ["gepa", "foo"]


def test_frontmatter_yaml_no_frontmatter_returns_empty():
    assert frontmatter_yaml("no frontmatter here") == {}


def test_frontmatter_yaml_unterminated_returns_empty():
    assert frontmatter_yaml("---\nname: x\nnever closes\n") == {}


def test_frontmatter_yaml_malformed_does_not_raise():
    # invalid YAML inside the block — must degrade to {} (runs at boot).
    assert frontmatter_yaml("---\nfoo: [unclosed\n---\n") == {}


# ── required_extras() ────────────────────────────────────────────────


def test_required_extras_unions_and_sorts(tmp_path: Path):
    skills = tmp_path / "skills"
    _write_skill(skills, "gepa", "---\nname: gepa\nrequires_extras: [gepa]\n---\n")
    _write_skill(skills, "other", "---\nname: o\nrequires_extras:\n  - foo\n  - gepa\n---\n")
    _write_skill(skills, "plain", "---\nname: p\ndescription: none\n---\n")
    assert required_extras(tmp_path) == ["foo", "gepa"]


def test_required_extras_string_value_normalized(tmp_path: Path):
    _write_skill(tmp_path / "skills", "s", "---\nname: s\nrequires_extras: gepa\n---\n")
    assert required_extras(tmp_path) == ["gepa"]


def test_required_extras_empty_when_none_declared(tmp_path: Path):
    _write_skill(tmp_path / "skills", "s", "---\nname: s\ndescription: x\n---\n")
    assert required_extras(tmp_path) == []


def test_required_extras_no_skill_dirs(tmp_path: Path):
    assert required_extras(tmp_path) == []


def test_required_extras_skips_malformed_skill(tmp_path: Path):
    skills = tmp_path / "skills"
    _write_skill(skills, "good", "---\nname: good\nrequires_extras: [gepa]\n---\n")
    _write_skill(skills, "bad", "---\nfoo: [unclosed\n---\n")
    assert required_extras(tmp_path) == ["gepa"]


def test_required_extras_scans_builtin_dir_too(tmp_path: Path):
    _write_skill(tmp_path / ".mimir_builtin_skills", "b", "---\nname: b\nrequires_extras: [mcp]\n---\n")
    assert required_extras(tmp_path) == ["mcp"]


# ── cmd_required_extras output formats ───────────────────────────────


def _run_cmd(home: Path, *, as_uv_flags: bool, capsys) -> str:
    rc = cmd_required_extras(argparse.Namespace(home=home, as_uv_flags=as_uv_flags))
    assert rc == 0
    return capsys.readouterr().out


def test_cmd_required_extras_lines(tmp_path: Path, capsys):
    _write_skill(tmp_path / "skills", "g", "---\nname: g\nrequires_extras: [gepa]\n---\n")
    assert _run_cmd(tmp_path, as_uv_flags=False, capsys=capsys).strip() == "gepa"


def test_cmd_required_extras_as_uv_flags(tmp_path: Path, capsys):
    skills = tmp_path / "skills"
    _write_skill(skills, "g", "---\nname: g\nrequires_extras: [gepa]\n---\n")
    _write_skill(skills, "m", "---\nname: m\nrequires_extras: [mcp]\n---\n")
    assert _run_cmd(tmp_path, as_uv_flags=True, capsys=capsys).strip() == "--extra gepa --extra mcp"


def test_cmd_required_extras_empty_prints_nothing(tmp_path: Path, capsys):
    assert _run_cmd(tmp_path, as_uv_flags=True, capsys=capsys) == ""


# ── CI guard: declared extras must be real pyproject extras ──────────


def test_bundled_skills_requires_extras_are_real_pyproject_extras():
    """A skill's ``requires_extras`` must name a real
    ``[project.optional-dependencies]`` key — otherwise start.sh's
    ``uv sync --extra X`` fails at boot. Validated at CI, not runtime
    (editable installs report stale ``Provides-Extra``)."""
    repo = Path(__file__).parent.parent
    pyproject = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
    declared = set(pyproject["project"]["optional-dependencies"])
    offenders: dict[str, list[str]] = {}
    for root in (repo / "mimir" / "skills", repo / "mimir" / "optional-skills"):
        if not root.is_dir():
            continue
        for skill_md in sorted(root.glob("*/SKILL.md")):
            raw = frontmatter_yaml(skill_md.read_text(encoding="utf-8")).get("requires_extras")
            if isinstance(raw, str):
                raw = [raw]
            for extra in raw or []:
                if extra not in declared:
                    offenders.setdefault(skill_md.parent.name, []).append(extra)
    assert not offenders, f"requires_extras not declared in pyproject extras: {offenders}"


def test_gepa_skill_declares_gepa_extra():
    """gepa is the first consumer of requires_extras (chainlink #406)."""
    repo = Path(__file__).parent.parent
    skill_md = repo / "mimir" / "optional-skills" / "gepa" / "SKILL.md"
    assert frontmatter_yaml(skill_md.read_text(encoding="utf-8")).get("requires_extras") == ["gepa"]


# ── scaffold start.sh folds in the resolver ──────────────────────────


def test_workspace_start_sh_folds_in_skill_required_extras():
    from mimir.scaffold_docker import render_start_sh

    out = render_start_sh(uv_extras=["discord"])
    assert "mimir skills required-extras" in out
    assert "--as-uv-flags" in out
    assert "uv sync $UV_EXTRAS $SKILL_EXTRAS" in out


def test_pypi_start_sh_has_no_resolver_call():
    """PyPI mode has no boot-time uv sync, so no resolver call (extras flow
    through the image build-arg there)."""
    from mimir.scaffold_docker import render_start_sh

    assert "required-extras" not in render_start_sh(mode="pypi")
