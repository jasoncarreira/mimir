"""Skill-declared required extras + start.sh resolver (chainlink #406)."""

from __future__ import annotations

import argparse
import tomllib
from pathlib import Path

from mimir.skill_install import cmd_required_extras, required_extras
from mimir.skill_md import frontmatter_list_field


def _write_skill(skills_dir: Path, name: str, frontmatter: str) -> None:
    d = skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(frontmatter, encoding="utf-8")


# ── frontmatter_list_field ───────────────────────────────────────────


def test_list_field_inline():
    text = "---\nname: x\nrequires_extras: [gepa, foo]\n---\nbody\n"
    assert frontmatter_list_field(text, "requires_extras") == ["gepa", "foo"]


def test_list_field_block():
    text = "---\nname: x\nrequires_extras:\n  - gepa\n  - foo\n---\nbody\n"
    assert frontmatter_list_field(text, "requires_extras") == ["gepa", "foo"]


def test_list_field_scalar_normalized():
    text = "---\nname: x\nrequires_extras: gepa\n---\n"
    assert frontmatter_list_field(text, "requires_extras") == ["gepa"]


def test_list_field_absent():
    assert frontmatter_list_field("---\nname: x\n---\n", "requires_extras") == []


def test_list_field_no_frontmatter():
    assert frontmatter_list_field("no frontmatter", "requires_extras") == []


def test_list_field_unterminated():
    assert frontmatter_list_field("---\nname: x\nnever closes\n", "requires_extras") == []


def test_list_field_ignores_bare_colon_description():
    """#406 review: an unquoted description with a bare ``: `` is valid under
    the flat frontmatter contract but breaks whole-block yaml. Parsing only the
    target field must still surface requires_extras."""
    text = (
        "---\nname: x\n"
        "description: Use when: this description has a bare colon\n"
        "requires_extras: [gepa, foo]\n---\nbody\n"
    )
    assert frontmatter_list_field(text, "requires_extras") == ["gepa", "foo"]


# ── required_extras() ────────────────────────────────────────────────


def test_required_extras_unions_and_sorts(tmp_path: Path):
    skills = tmp_path / "skills"
    _write_skill(skills, "gepa", "---\nname: gepa\nrequires_extras: [gepa]\n---\n")
    _write_skill(skills, "other", "---\nname: o\nrequires_extras:\n  - foo\n  - gepa\n---\n")
    _write_skill(skills, "plain", "---\nname: p\ndescription: none\n---\n")
    assert required_extras(tmp_path) == ["foo", "gepa"]


def test_required_extras_survives_bare_colon_description(tmp_path: Path):
    """Regression for the #406 review repro: a skill whose unquoted description
    contains a bare ``: `` must still contribute its requires_extras."""
    _write_skill(
        tmp_path / "skills",
        "colon",
        "---\nname: colon\n"
        "description: Use when: this description has a bare colon\n"
        "requires_extras: [gepa]\n---\nbody\n",
    )
    assert required_extras(tmp_path) == ["gepa"]


def test_required_extras_empty_when_none_declared(tmp_path: Path):
    _write_skill(tmp_path / "skills", "s", "---\nname: s\ndescription: x\n---\n")
    assert required_extras(tmp_path) == []


def test_required_extras_no_skill_dirs(tmp_path: Path):
    assert required_extras(tmp_path) == []


def test_required_extras_skips_skill_without_skill_md(tmp_path: Path):
    (tmp_path / "skills" / "draft").mkdir(parents=True)
    _write_skill(tmp_path / "skills", "good", "---\nname: good\nrequires_extras: [gepa]\n---\n")
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
            for extra in frontmatter_list_field(skill_md.read_text(encoding="utf-8"), "requires_extras"):
                if extra not in declared:
                    offenders.setdefault(skill_md.parent.name, []).append(extra)
    assert not offenders, f"requires_extras not declared in pyproject extras: {offenders}"


def test_gepa_skill_declares_gepa_extra():
    """gepa is the first consumer of requires_extras (chainlink #406)."""
    repo = Path(__file__).parent.parent
    skill_md = repo / "mimir" / "optional-skills" / "gepa" / "SKILL.md"
    assert frontmatter_list_field(skill_md.read_text(encoding="utf-8"), "requires_extras") == ["gepa"]


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
