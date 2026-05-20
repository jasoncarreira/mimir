"""Tests for ``mimir.scaffold_docker`` and the ``mimir scaffold-docker``
CLI.

Covers:
- fragment collection (home wins; bundled fallback for ungraded homes)
- pollers.json env-var collection
- compose.env idempotency (commented placeholders count as 'present',
  operator values survive re-runs, new pollers append only missing keys)
- the Dockerfile sentinel-block + skill fragment ordering
- end-to-end ``scaffold(home)`` orchestration on a fresh + populated home
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mimir.scaffold_docker import (
    Fragment,
    collect_fragments,
    collect_required_env_vars,
    existing_env_keys,
    render_compose_env,
    render_dockerfile,
    scaffold,
)


# ── fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def home_with_two_skills(tmp_path: Path) -> Path:
    """Mimir home with two installed skills, one with a fragment, one
    without. Sufficient for fragment + env-var collection tests."""
    home = tmp_path / "home"
    skills = home / ".claude" / "skills"
    skills.mkdir(parents=True)

    # Skill A: has dockerfile.fragment + pollers.json
    a = skills / "skill-a"
    a.mkdir()
    (a / "SKILL.md").write_text("---\nname: skill-a\ndescription: A\n---\nbody")
    (a / "dockerfile.fragment").write_text("RUN echo skill-a\n")
    (a / "pollers.json").write_text(
        '{"pollers": [{"name": "a", "command": "true", "pass_env": ["FOO", "BAR"]}]}'
    )

    # Skill B: no fragment, no pollers.json
    b = skills / "skill-b"
    b.mkdir()
    (b / "SKILL.md").write_text("---\nname: skill-b\ndescription: B\n---\nbody")

    return home


# ── collect_fragments ───────────────────────────────────────────────


def test_collect_fragments_picks_up_installed_fragment(home_with_two_skills: Path):
    frags = collect_fragments(home_with_two_skills)
    assert len(frags) == 1
    assert frags[0].skill_name == "skill-a"
    assert frags[0].content == "RUN echo skill-a"


def test_collect_fragments_falls_back_to_bundled(tmp_path: Path, monkeypatch):
    """When a skill is present in the home but lacks a dockerfile.fragment
    locally, the scaffolder must look in the bundled source (mimir/skills/
    or optional-skills/) as a backstop for homes seeded before fragments
    existed in the bundle.
    """
    # Fake bundled root with a fragment for ``mimir/skills/skill-c``.
    bundled = tmp_path / "bundled"
    (bundled / "skill-c").mkdir(parents=True)
    (bundled / "skill-c" / "dockerfile.fragment").write_text("RUN bundled-c")

    # Home with skill-c installed but NO fragment of its own.
    home = tmp_path / "home"
    skills = home / ".claude" / "skills"
    (skills / "skill-c").mkdir(parents=True)
    (skills / "skill-c" / "SKILL.md").write_text(
        "---\nname: skill-c\ndescription: C\n---\nbody"
    )

    monkeypatch.setattr(
        "mimir.scaffold_docker._BUNDLED_SKILL_ROOTS",
        (bundled,),
    )
    frags = collect_fragments(home)
    assert len(frags) == 1
    assert frags[0].skill_name == "skill-c"
    assert frags[0].content == "RUN bundled-c"


def test_collect_fragments_empty_home(tmp_path: Path):
    assert collect_fragments(tmp_path / "no-such-home") == []


def test_collect_fragments_ordered_by_skill_name(tmp_path: Path):
    """Stable Dockerfile output across runs: fragments come out
    alphabetically by skill name."""
    home = tmp_path / "home"
    skills = home / ".claude" / "skills"
    skills.mkdir(parents=True)
    for name in ("z-skill", "a-skill", "m-skill"):
        d = skills / name
        d.mkdir()
        (d / "SKILL.md").write_text(f"---\nname: {name}\n---\nbody")
        (d / "dockerfile.fragment").write_text(f"RUN {name}\n")
    frags = collect_fragments(home)
    assert [f.skill_name for f in frags] == ["a-skill", "m-skill", "z-skill"]


# ── collect_required_env_vars ───────────────────────────────────────


def test_collect_required_env_vars_includes_baseline(home_with_two_skills: Path):
    keys = collect_required_env_vars(home_with_two_skills)
    assert "MIMIR_API_KEY" in keys
    assert "VOYAGE_API_KEY" in keys
    assert "GITHUB_TOKEN" in keys


def test_collect_required_env_vars_appends_poller_pass_env(
    home_with_two_skills: Path,
):
    keys = collect_required_env_vars(home_with_two_skills)
    # skill-a's pollers.json adds FOO + BAR
    assert "FOO" in keys
    assert "BAR" in keys


def test_collect_required_env_vars_dedupes(tmp_path: Path):
    """If two pollers both declare the same pass_env entry, it appears
    only once in the result."""
    home = tmp_path / "home"
    skills = home / ".claude" / "skills"
    skills.mkdir(parents=True)
    for name in ("a", "b"):
        d = skills / name
        d.mkdir()
        (d / "SKILL.md").write_text(f"---\nname: {name}\n---\n")
        (d / "pollers.json").write_text(
            '{"pollers": [{"name": "' + name + '", "command": "true", '
            '"pass_env": ["SHARED_KEY"]}]}'
        )
    keys = collect_required_env_vars(home)
    assert keys.count("SHARED_KEY") == 1


# ── existing_env_keys ───────────────────────────────────────────────


def test_existing_env_keys_picks_up_live_settings():
    keys = existing_env_keys("FOO=value\nBAR=other\n")
    assert keys == {"FOO", "BAR"}


def test_existing_env_keys_picks_up_commented_placeholders():
    """Commented placeholders (`# KEY=`) MUST count as 'key already
    present'. Otherwise scaffold-docker would re-append every placeholder
    on every run."""
    keys = existing_env_keys("# FOO=\n# BAR=\nLIVE=val\n")
    assert keys == {"FOO", "BAR", "LIVE"}


def test_existing_env_keys_ignores_random_comments():
    keys = existing_env_keys("# This is a header\n# Another comment\nFOO=v\n")
    assert keys == {"FOO"}


# ── render_compose_env idempotency ─────────────────────────────────


def test_render_compose_env_fresh_emits_all_keys():
    text, added = render_compose_env(None, ["FOO", "BAR"])
    assert "# FOO=" in text
    assert "# BAR=" in text
    assert set(added) == {"FOO", "BAR"}


def test_render_compose_env_preserves_operator_values():
    existing = "FOO=secret_value\n# BAR=\n"
    text, added = render_compose_env(existing, ["FOO", "BAR", "BAZ"])
    assert "FOO=secret_value" in text
    assert "# BAR=" in text  # was already there, untouched
    assert "BAZ" in text  # newly appended
    assert added == ["BAZ"]


def test_render_compose_env_no_op_when_all_present():
    """All keys already in the file (live OR commented) → return
    unchanged + empty added list."""
    existing = "FOO=v\n# BAR=\n"
    text, added = render_compose_env(existing, ["FOO", "BAR"])
    assert text == existing
    assert added == []


def test_render_compose_env_idempotent_on_re_run():
    """Running render_compose_env back-to-back on its own output must
    not keep appending. Tests the full round-trip."""
    text1, _ = render_compose_env(None, ["FOO", "BAR"])
    text2, added2 = render_compose_env(text1, ["FOO", "BAR"])
    assert added2 == []
    assert text1 == text2


# ── render_dockerfile ──────────────────────────────────────────────


def test_render_dockerfile_inserts_fragments():
    frags = [
        Fragment(skill_name="a", content="RUN echo a"),
        Fragment(skill_name="b", content="RUN echo b"),
    ]
    out = render_dockerfile(frags)
    assert "RUN echo a" in out
    assert "RUN echo b" in out
    assert "BEGIN mimir-scaffold-docker: skill fragments" in out
    assert "END mimir-scaffold-docker: skill fragments" in out
    # Fragments labeled with their skill names so the generated file is readable.
    assert "# --- a ---" in out
    assert "# --- b ---" in out


def test_render_dockerfile_empty_fragments():
    out = render_dockerfile([])
    assert "no skills installed yet ship a dockerfile.fragment" in out
    assert "BEGIN mimir-scaffold-docker" in out


def test_render_dockerfile_has_base_layer():
    """Sanity: regardless of fragments, the base image + tooling are
    present (git, gh, uv, claude-code, mermaid)."""
    out = render_dockerfile([])
    assert "FROM python:3.11-slim" in out
    assert "@anthropic-ai/claude-code" in out
    assert "astral.sh/uv/install.sh" in out


# ── scaffold() end-to-end ──────────────────────────────────────────


def test_scaffold_writes_all_four_files(home_with_two_skills: Path):
    result = scaffold(home_with_two_skills)
    for fname in ("Dockerfile", "compose.yml", "compose.env", "start.sh"):
        assert (home_with_two_skills / fname).is_file(), \
            f"{fname} not written"
    assert "Dockerfile" in result.files_written
    assert "compose.yml" in result.files_written
    assert "start.sh" in result.files_written


def test_scaffold_start_sh_is_executable(home_with_two_skills: Path):
    scaffold(home_with_two_skills)
    import stat
    mode = (home_with_two_skills / "start.sh").stat().st_mode
    assert mode & stat.S_IXUSR, "start.sh must be executable"


def test_scaffold_picks_up_skills(home_with_two_skills: Path):
    result = scaffold(home_with_two_skills)
    assert result.skills_with_fragments == ["skill-a"]


def test_scaffold_idempotent_compose_env(home_with_two_skills: Path):
    """Re-running scaffold-docker on the same home must NOT append
    duplicate env-var placeholders."""
    result1 = scaffold(home_with_two_skills)
    assert len(result1.env_vars_added) > 0

    env_file = home_with_two_skills / "compose.env"
    text_after_first = env_file.read_text()

    result2 = scaffold(home_with_two_skills)
    text_after_second = env_file.read_text()

    assert text_after_first == text_after_second
    assert result2.env_vars_added == []
    assert "compose.env (no changes)" in result2.files_skipped


def test_scaffold_picks_up_new_skill_after_install(
    home_with_two_skills: Path,
):
    """Adding a new skill (with a fragment + pass_env) and re-running
    scaffold-docker must add ITS fragment to the Dockerfile and ITS
    env vars to compose.env — without touching what's already there.
    """
    home = home_with_two_skills
    result1 = scaffold(home)
    df_before = (home / "Dockerfile").read_text()
    env_before = (home / "compose.env").read_text()

    # Operator sets a value in compose.env.
    env_text = env_before.replace("# FOO=", "FOO=operator_value")
    (home / "compose.env").write_text(env_text)

    # Install a new skill with its own fragment + pollers.json.
    new = home / ".claude" / "skills" / "skill-new"
    new.mkdir()
    (new / "SKILL.md").write_text("---\nname: skill-new\n---\n")
    (new / "dockerfile.fragment").write_text("RUN echo skill-new")
    (new / "pollers.json").write_text(
        '{"pollers": [{"name": "n", "command": "true", "pass_env": ["NEW_KEY"]}]}'
    )

    result2 = scaffold(home)
    df_after = (home / "Dockerfile").read_text()
    env_after = (home / "compose.env").read_text()

    # Dockerfile picks up the new fragment.
    assert "skill-new" in result2.skills_with_fragments
    assert "RUN echo skill-new" in df_after
    # Original fragment also still there.
    assert "RUN echo skill-a" in df_after
    # compose.env preserves the operator's value AND appends only the new key.
    assert "FOO=operator_value" in env_after
    assert "# NEW_KEY=" in env_after
    assert result2.env_vars_added == ["NEW_KEY"]


def test_scaffold_missing_home_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        scaffold(tmp_path / "does-not-exist")


# ── Dockerfile blocker — broken PATH ─────────────────────────────────


def test_dockerfile_has_valid_path_env(home_with_two_skills: Path):
    """Mimir #225 review caught ``${{PATH}}`` shipping literally in
    the Dockerfile (because the template was rendered via .replace()
    not .format()). Docker doesn't understand ${{VAR}}; the resulting
    container had a broken PATH. Regression: no double-braces.
    """
    scaffold(home_with_two_skills)
    df = (home_with_two_skills / "Dockerfile").read_text()
    assert "${{PATH}}" not in df, (
        "Dockerfile shipped literal ${{PATH}} — Docker doesn't parse "
        "that as a variable reference; container PATH ends up broken."
    )
    assert "${PATH}" in df, "expected Docker-style ${PATH} expansion"


# ── render_compose_yml() ─────────────────────────────────────────────


def test_render_compose_yml_substitutes_service_name():
    from mimir.scaffold_docker import render_compose_yml
    out = render_compose_yml(service_name="muninn-mimir", web_port=8091)
    assert "container_name: muninn-mimir" in out
    assert "muninn-mimir:" in out  # service key line
    assert "127.0.0.1:8091:8080" in out


def test_render_compose_yml_no_unresolved_placeholders():
    from mimir.scaffold_docker import render_compose_yml
    out = render_compose_yml(service_name="x", web_port=1234)
    # No leftover template tokens like {SERVICE_NAME} / {WEB_PORT}.
    assert "{SERVICE_NAME}" not in out
    assert "{WEB_PORT}" not in out


# ── render_start_sh() ────────────────────────────────────────────────


def test_render_start_sh_no_extras_by_default():
    from mimir.scaffold_docker import render_start_sh
    out = render_start_sh()
    # Empty UV_EXTRAS = no --extra flags expanded.
    assert "UV_EXTRAS=\"\"" in out
    assert "{UV_EXTRAS}" not in out


def test_render_start_sh_expands_extras():
    from mimir.scaffold_docker import render_start_sh
    out = render_start_sh(uv_extras=["discord", "claude-code"])
    assert "UV_EXTRAS=\"--extra discord --extra claude-code\"" in out


def test_render_start_sh_uv_sync_line_is_valid_shell_with_no_extras():
    """Regression: the prior template had ``uv sync ${UV_EXTRAS}`` on
    the invocation line. The Python renderer's
    ``.replace("{UV_EXTRAS}", flags)`` matched the ``{UV_EXTRAS}``
    substring INSIDE ``${UV_EXTRAS}`` and replaced it with the empty
    string when no extras were configured — producing ``uv sync $``
    (literal stray dollar, no variable). That crashed every
    container at boot with ``error: unexpected argument '$' found``.

    The fix uses bare ``$UV_EXTRAS`` shell expansion on the
    invocation line so the ``{UV_EXTRAS}`` substring never appears
    next to a leading ``$`` for the renderer to clobber.

    Caught during muninn-mimir cutover on 2026-05-20."""
    from mimir.scaffold_docker import render_start_sh
    out = render_start_sh()
    # The stray-$ bug would produce this exact line; if it shows up
    # we've regressed.
    assert "uv sync $\n" not in out
    assert "uv sync $ " not in out
    # The fix renders the invocation as bare shell expansion which
    # still works correctly when UV_EXTRAS is empty.
    assert "uv sync $UV_EXTRAS" in out


# ── _resolve_home() precedence ───────────────────────────────────────


def test_resolve_home_arg_wins(tmp_path: Path, monkeypatch):
    from mimir.scaffold_docker import _resolve_home
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path / "from-env"))
    arg_home = tmp_path / "from-arg"
    arg_home.mkdir()
    assert _resolve_home(arg_home) == arg_home.resolve()


def test_resolve_home_env_used_when_no_arg(tmp_path: Path, monkeypatch):
    from mimir.scaffold_docker import _resolve_home
    env_home = tmp_path / "from-env"
    env_home.mkdir()
    monkeypatch.setenv("MIMIR_HOME", str(env_home))
    assert _resolve_home(None) == env_home.resolve()


def test_resolve_home_cwd_fallback(tmp_path: Path, monkeypatch):
    from mimir.scaffold_docker import _resolve_home
    monkeypatch.delenv("MIMIR_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    assert _resolve_home(None) == tmp_path.resolve()


# ── cmd() CLI handler ────────────────────────────────────────────────


def test_cmd_rejects_invalid_web_port(home_with_two_skills: Path, capsys):
    """--web-port 99999 must error cleanly, not silently emit a broken
    compose.yml."""
    from argparse import Namespace
    from mimir.scaffold_docker import cmd
    args = Namespace(
        home=home_with_two_skills, service_name=None,
        web_port=99999, uv_extras="",
    )
    rc = cmd(args)
    assert rc == 2
    assert "out of range" in capsys.readouterr().out


def test_cmd_returns_2_for_missing_home(tmp_path: Path, capsys):
    from argparse import Namespace
    from mimir.scaffold_docker import cmd
    args = Namespace(
        home=tmp_path / "does-not-exist", service_name=None,
        web_port=8090, uv_extras="",
    )
    rc = cmd(args)
    assert rc == 2
    assert "not a directory" in capsys.readouterr().out


def test_cmd_passes_extras_through(home_with_two_skills: Path, capsys):
    """--uv-extras csv parses correctly and lands in start.sh."""
    from argparse import Namespace
    from mimir.scaffold_docker import cmd
    args = Namespace(
        home=home_with_two_skills, service_name=None,
        web_port=8090, uv_extras="discord, claude-code",
    )
    rc = cmd(args)
    assert rc == 0
    start_sh = (home_with_two_skills / "start.sh").read_text()
    assert "--extra discord --extra claude-code" in start_sh


# ── Service name sanitization ────────────────────────────────────────


def test_scaffold_sanitizes_home_dir_name_for_service_name(tmp_path: Path):
    """Home dir with spaces / special chars → safe container name."""
    weird = tmp_path / "My Weird Home!"
    (weird / ".claude" / "skills").mkdir(parents=True)
    result = scaffold(weird)
    cy = (weird / "compose.yml").read_text()
    # Allowed chars: alnum + hyphen + underscore; spaces / ! become hyphens.
    assert "container_name: My-Weird-Home-" in cy
    # No raw spaces in the service name.
    for line in cy.splitlines():
        if line.lstrip().startswith("container_name"):
            assert " " not in line.split(":", 1)[1].strip()


def test_scaffold_explicit_service_name_overrides_home_name(tmp_path: Path):
    """Explicit service name wins over the home-dir-derived default."""
    weird = tmp_path / "My Weird Home!"
    (weird / ".claude" / "skills").mkdir(parents=True)
    scaffold(weird, service_name="muninn")
    cy = (weird / "compose.yml").read_text()
    assert "container_name: muninn" in cy


def test_scaffold_explicit_service_name_is_also_sanitized(tmp_path: Path, capsys):
    """Operator typo in --service-name (spaces, slashes) gets fixed
    instead of producing an invalid compose service name."""
    weird = tmp_path / "agent-home"
    (weird / ".claude" / "skills").mkdir(parents=True)
    scaffold(weird, service_name="muninn alpha/v2")
    cy = (weird / "compose.yml").read_text()
    assert "container_name: muninn-alpha-v2" in cy
    # And the operator gets told about the rewrite so it isn't silent.
    captured = capsys.readouterr().out
    assert "muninn alpha/v2" in captured
    assert "muninn-alpha-v2" in captured


# ── .gitignore belt-and-suspenders ───────────────────────────────────


def test_scaffold_appends_compose_env_to_gitignore(home_with_two_skills: Path):
    """compose.env contains secrets and should never be committed.
    The scaffolder adds a never-track entry as belt-and-suspenders for
    operators who switch from the default allowlist to a blocklist."""
    home = home_with_two_skills
    scaffold(home)
    gi = (home / ".gitignore").read_text()
    assert "compose.env" in gi
    assert "mimir-scaffold-docker" in gi  # sentinel comment


def test_scaffold_does_not_double_append_gitignore(home_with_two_skills: Path):
    """Re-running must not duplicate the .gitignore entry."""
    home = home_with_two_skills
    scaffold(home)
    gi_first = (home / ".gitignore").read_text()
    scaffold(home)
    gi_second = (home / ".gitignore").read_text()
    assert gi_first == gi_second
    assert gi_second.count("mimir-scaffold-docker") == 1


# ── Malformed pollers.json defensive handling ────────────────────────


def test_collect_required_env_vars_survives_malformed_pollers_json(tmp_path: Path):
    """A pollers.json that's valid JSON but wrong-shape must not
    crash collection. Mimir #225 review flagged: ``"pollers": "oops"``
    would raise TypeError on the next ``.get()`` chain."""
    home = tmp_path / "home"
    skills = home / ".claude" / "skills"
    skills.mkdir(parents=True)
    s = skills / "broken-skill"
    s.mkdir()
    (s / "SKILL.md").write_text("---\nname: broken\n---\n")
    (s / "pollers.json").write_text('{"pollers": "oops"}')
    keys = collect_required_env_vars(home)
    # Must still return the baseline keys, not crash.
    assert "MIMIR_API_KEY" in keys


def test_collect_required_env_vars_skips_non_string_pass_env(tmp_path: Path):
    """If pass_env is a list but contains non-strings, those entries
    are skipped (not crashed-on)."""
    home = tmp_path / "home"
    skills = home / ".claude" / "skills"
    skills.mkdir(parents=True)
    s = skills / "weird-skill"
    s.mkdir()
    (s / "SKILL.md").write_text("---\nname: weird\n---\n")
    (s / "pollers.json").write_text(
        '{"pollers": [{"name": "w", "pass_env": ["GOOD_KEY", 42, null]}]}'
    )
    keys = collect_required_env_vars(home)
    assert "GOOD_KEY" in keys
    # 42 and null shouldn't crash and shouldn't end up in the list.
    assert 42 not in keys


# ── Sentinel-split fragment-insert robustness ────────────────────────


def test_render_dockerfile_handles_sentinel_in_fragment(tmp_path: Path):
    """A fragment containing the literal sentinel string must not
    corrupt the insertion (sentinel split + partition is robust)."""
    sentinel = "<!-- mimir-scaffold-docker:FRAGMENTS -->"
    frag = Fragment(
        skill_name="evil",
        content=f"# Has the sentinel: {sentinel}\nRUN echo evil",
    )
    out = render_dockerfile([frag])
    # Output should still have ONE BEGIN/END pair, not multiple.
    assert out.count("BEGIN mimir-scaffold-docker") == 1
    assert out.count("END mimir-scaffold-docker") == 1
    # The fragment contents (including the embedded sentinel literal)
    # should be present somewhere.
    assert "RUN echo evil" in out


# ── Re-review (#225 second round) regression tests ───────────────────


def test_dockerfile_header_does_not_falsely_claim_preservation(home_with_two_skills):
    """Mimir #225 re-review caught the Dockerfile header comment
    promising "operator edits OUTSIDE the sentinel-marked blocks are
    preserved" — but scaffold() does df_path.write_text() (full regen)
    so the claim was false and would silently lose operator edits.
    Header must now warn that the file is fully regenerated.
    """
    scaffold(home_with_two_skills)
    df = (home_with_two_skills / "Dockerfile").read_text()
    # Regression: must not promise preservation.
    assert "are preserved across regenerations" not in df
    # Must warn about regeneration.
    assert "REGENERATED IN" in df
    assert "do NOT edit" in df


def test_compose_yml_header_warns_about_regen(home_with_two_skills):
    scaffold(home_with_two_skills)
    cy = (home_with_two_skills / "compose.yml").read_text()
    assert "REGENERATED IN" in cy
    assert "WILL be lost" in cy


def test_start_sh_header_warns_about_regen(home_with_two_skills):
    scaffold(home_with_two_skills)
    ss = (home_with_two_skills / "start.sh").read_text()
    assert "REGENERATED IN" in ss
    assert "do NOT edit" in ss


def test_scaffold_idempotent_reporting_no_changes(home_with_two_skills):
    """Mimir #225 re-review: ``files_written`` was reporting Dockerfile
    / compose.yml / start.sh on EVERY run, even when the rendered
    content was byte-identical to what was already on disk. For an
    idempotent tool, "written" should mean "something changed."
    """
    # First run — everything is new.
    r1 = scaffold(home_with_two_skills)
    assert "Dockerfile" in r1.files_written
    assert "compose.yml" in r1.files_written
    assert "start.sh" in r1.files_written

    # Second run — no inputs changed → nothing should be reported as written.
    r2 = scaffold(home_with_two_skills)
    assert r2.files_written == [], (
        f"expected no files written on re-run, got: {r2.files_written}"
    )
    # All three should land in skipped instead.
    skipped_labels = {s.split(" ")[0] for s in r2.files_skipped}
    for f in ("Dockerfile", "compose.yml", "start.sh"):
        assert f in skipped_labels, f"{f} should be in skipped on idempotent re-run"


def test_scaffold_reports_only_changed_files(home_with_two_skills):
    """If only one input changes between runs, only that file should
    appear in ``files_written`` — the others stay in ``files_skipped``.
    """
    scaffold(home_with_two_skills)
    # Install a new skill with a fragment → Dockerfile changes; others don't.
    new = home_with_two_skills / ".claude" / "skills" / "newcomer"
    new.mkdir()
    (new / "SKILL.md").write_text("---\nname: newcomer\n---\n")
    (new / "dockerfile.fragment").write_text("RUN echo newcomer")

    r = scaffold(home_with_two_skills)
    assert "Dockerfile" in r.files_written
    assert "compose.yml" not in r.files_written
    assert "start.sh" not in r.files_written


def test_start_sh_chmod_persists_through_no_change_runs(home_with_two_skills):
    """The chmod call is unconditional (separate from write_if_changed)
    so a previously-corrupted mode bit gets restored even when content
    is unchanged. Sanity check: after an idempotent re-run, start.sh
    is still executable.
    """
    import stat
    scaffold(home_with_two_skills)
    ss = home_with_two_skills / "start.sh"
    # Drop the exec bit manually.
    ss.chmod(0o644)
    assert not (ss.stat().st_mode & stat.S_IXUSR)
    # Re-run with no input changes — content stays same, mode bit gets restored.
    scaffold(home_with_two_skills)
    assert ss.stat().st_mode & stat.S_IXUSR


def test_baseline_env_includes_mimir_git_url(home_with_two_skills, capsys):
    """Mimir #225 re-review: MIMIR_GIT_URL is referenced in start.sh
    via ${MIMIR_GIT_URL:-default} so non-mimir agent homes (Muninn,
    forks) can override the clone source — but it wasn't in the
    baseline keys list, so compose.env never templated a placeholder
    for it. Operators got a silent footgun.
    """
    scaffold(home_with_two_skills)
    env = (home_with_two_skills / "compose.env").read_text()
    assert "MIMIR_GIT_URL" in env, (
        "MIMIR_GIT_URL must be in compose.env's baseline placeholders "
        "so operators forking the source can find the env var to set."
    )


def test_start_sh_has_mimir_git_url_inline_doc(home_with_two_skills):
    """The MIMIR_GIT_URL reference in start.sh should carry an inline
    comment explaining when to override (e.g. for Muninn-like forks)."""
    scaffold(home_with_two_skills)
    ss = (home_with_two_skills / "start.sh").read_text()
    # The line right before the REPO_URL assignment should mention
    # MIMIR_GIT_URL and forks.
    assert "MIMIR_GIT_URL" in ss
    assert "fork" in ss.lower() or "override" in ss.lower()


def test_gmail_fragment_has_bump_comment():
    """Mimir nit: hardcoded Go version should carry a bump-this comment
    so future maintainers know it's a pin, not a load-bearing constant."""
    frag_path = (
        Path(__file__).parent.parent
        / "optional-skills" / "gmail-poller" / "dockerfile.fragment"
    )
    text = frag_path.read_text()
    # Some form of "bump" / "update" / "check" guidance near the version.
    assert "Go version" in text
    assert "Bump" in text or "bump" in text


def test_social_cli_fragment_has_pin_comment():
    """Mimir nit: cloning untagged main is non-deterministic — flag it
    so a future maintainer pins when upstream stabilizes."""
    frag_path = (
        Path(__file__).parent.parent
        / "optional-skills" / "social-cli-poller" / "dockerfile.fragment"
    )
    text = frag_path.read_text()
    assert "pin" in text.lower() or "tag" in text.lower()
