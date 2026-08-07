"""Per-job shell grants declared where the job itself is defined.

mimir ships no catalogue of the CLIs a deployment might install. A scheduled job
or poller declares the commands it needs, and what stays in code is the SHAPE a
declaration must take. These tests pin that shape, because it is the whole
security content of the feature.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mimir import access_control
from mimir.access_control import (
    DeclaredShellCommand,
    ServiceShellBindingRule,
    agent_writable_roots,
    parse_declared_shell_commands,
    parse_service_shell_argv,
    parse_service_shell_argv_with_diagnostics,
)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A home with an operator-owned scripts/ and an agent-writable scratch/."""
    for name in ("scripts", "scratch", "skills", "state"):
        (tmp_path / name).mkdir()
    (tmp_path / "scripts" / "todo.py").write_text("print(1)\n")
    (tmp_path / "scratch" / "evil.py").write_text("print(1)\n")
    (tmp_path / "skills" / "bundled.py").write_text("print(1)\n")
    return tmp_path


def _gog(**over):
    entry = {
        "exec": "gog",
        "path": "/bin/echo",
        "subcommands": [["gmail", "search"], ["calendar", "events"]],
        "options": ["--json", "--limit"],
    }
    entry.update(over)
    return entry


def test_declared_read_subcommands_are_admitted(home: Path) -> None:
    declared = parse_declared_shell_commands([_gog()], writable_roots=())
    for command in (
        "gog gmail search newer_than:24h",
        "gog calendar events --json",
        "gog gmail search --limit 5 some free query text",
    ):
        assert parse_service_shell_argv(
            command, "maintenance", declared=declared,
        ) is not None, command


def test_the_same_binary_is_refused_for_a_mutating_subcommand(home: Path) -> None:
    """A grant names subcommands, never a bare binary.

    ``gog gmail search`` and ``gog gmail send`` are one executable. Declaring the
    binary would grant both, which is why ``subcommands`` is required.
    """
    declared = parse_declared_shell_commands([_gog()], writable_roots=())
    for command in (
        "gog gmail send --to a@example.com",
        "gog auth add",
        "gog gmail delete abc123",
    ):
        assert parse_service_shell_argv(
            command, "maintenance", declared=declared,
        ) is None, command


def test_options_outside_the_allowlist_are_refused(home: Path) -> None:
    declared = parse_declared_shell_commands([_gog()], writable_roots=())
    assert parse_service_shell_argv(
        "gog gmail search --raw-output x", "maintenance", declared=declared,
    ) is None


def test_a_bare_binary_cannot_be_declared() -> None:
    with pytest.raises(ValueError, match="at least one subcommand"):
        parse_declared_shell_commands(
            [{"exec": "gog", "path": "/bin/echo"}], writable_roots=(),
        )


def test_declaration_is_additive_not_a_replacement(home: Path) -> None:
    """The profile is consulted first; a grant is a second chance, not a swap."""
    declared = parse_declared_shell_commands([_gog()], writable_roots=())
    argv, _reason, rule = parse_service_shell_argv_with_diagnostics(
        "gog gmail send --to a@b.com", "maintenance", declared=declared,
    )
    assert argv is None
    # Attribution distinguishes "this job declared commands, none matched" from
    # "the profile refused it", while ``destination`` keeps meaning the profile
    # so existing shadow-authz classification is untouched.
    assert rule is ServiceShellBindingRule.DECLARED_COMMAND_MISMATCH

    _argv, _reason, rule_without = parse_service_shell_argv_with_diagnostics(
        "gog gmail send --to a@b.com", "maintenance",
    )
    assert rule_without is ServiceShellBindingRule.PROFILE_ALLOWLIST


class TestInterpreterRule:
    """An interpreter is admitted only for a script the agent cannot rewrite."""

    def test_interpreter_requires_a_pinned_script(self) -> None:
        with pytest.raises(ValueError, match="only be declared with a pinned"):
            parse_declared_shell_commands(
                [{"exec": "python3", "path": "/usr/bin/python3",
                  "subcommands": [["anything"]]}],
                writable_roots=(),
            )

    def test_script_outside_writable_roots_is_admitted(self, home: Path) -> None:
        declared = parse_declared_shell_commands(
            [{"exec": "python3", "path": "/usr/bin/python3",
              "script": str(home / "scripts" / "todo.py")}],
            writable_roots=agent_writable_roots(home),
        )
        assert parse_service_shell_argv(
            f"python3 {home / 'scripts' / 'todo.py'}", "maintenance", declared=declared,
        ) is not None

    @pytest.mark.parametrize("subdir", ["scratch", "skills"])
    def test_script_inside_an_agent_writable_root_is_refused(
        self, home: Path, subdir: str,
    ) -> None:
        """``skills`` is rw, so a skill-bundled script is write-then-execute."""
        name = "evil.py" if subdir == "scratch" else "bundled.py"
        with pytest.raises(ValueError, match="agent-writable root"):
            parse_declared_shell_commands(
                [{"exec": "python3", "path": "/usr/bin/python3",
                  "script": str(home / subdir / name)}],
                writable_roots=agent_writable_roots(home),
            )

    def test_a_symlink_cannot_launder_the_script_path(self, home: Path) -> None:
        """Resolution happens before the check, or a link would defeat it."""
        link = home / "scripts" / "link.py"
        link.symlink_to(home / "scratch" / "evil.py")
        with pytest.raises(ValueError, match="agent-writable root"):
            parse_declared_shell_commands(
                [{"exec": "python3", "path": "/usr/bin/python3", "script": str(link)}],
                writable_roots=agent_writable_roots(home),
            )

    @pytest.mark.parametrize("option", ["-c", "-e", "-m", "--eval", "-"])
    def test_code_from_argv_options_are_never_declarable(self, option: str) -> None:
        with pytest.raises(ValueError, match="sources code from argv"):
            parse_declared_shell_commands(
                [{"exec": "gog", "path": "/bin/echo",
                  "subcommands": [["x"]], "options": [option]}],
                writable_roots=(),
            )

    def test_inline_code_is_refused_at_admission_too(self, home: Path) -> None:
        declared = parse_declared_shell_commands(
            [{"exec": "python3", "path": "/usr/bin/python3",
              "script": str(home / "scripts" / "todo.py")}],
            writable_roots=agent_writable_roots(home),
        )
        for command in ("python3 -c 'import os'", "python3 -m http.server",
                        f"python3 {home / 'scratch' / 'evil.py'}"):
            assert parse_service_shell_argv(
                command, "maintenance", declared=declared,
            ) is None, command


class TestDeclarationShape:
    def test_path_must_be_absolute_and_exist(self) -> None:
        with pytest.raises(ValueError, match="must be absolute"):
            parse_declared_shell_commands(
                [_gog(path="bin/echo")], writable_roots=(),
            )
        with pytest.raises(ValueError, match="does not exist"):
            parse_declared_shell_commands(
                [_gog(path="/nonexistent/gog")], writable_roots=(),
            )

    def test_exec_must_be_a_bare_name(self) -> None:
        with pytest.raises(ValueError, match="bare command name"):
            parse_declared_shell_commands(
                [_gog(exec="/usr/local/bin/gog")], writable_roots=(),
            )

    def test_unknown_keys_are_refused(self) -> None:
        with pytest.raises(ValueError, match="unknown keys"):
            parse_declared_shell_commands(
                [dict(_gog(), sudo=True)], writable_roots=(),
            )

    def test_the_pinned_path_is_what_executes(self) -> None:
        """A declaration is a pin: PATH never selects the binary.

        Compared against the RESOLVED path, because the declaration resolves
        symlinks before pinning — the writable-root check would otherwise be
        defeated by a link. On Linux ``/bin`` is a symlink to ``/usr/bin``, so a
        hardcoded ``/bin/echo`` passes on macOS and fails in CI.
        """
        declared = parse_declared_shell_commands([_gog()], writable_roots=())
        argv = parse_service_shell_argv(
            "gog gmail search x", "maintenance", declared=declared,
        )
        assert argv is not None
        assert argv[0] == str(Path("/bin/echo").resolve())
        assert Path(argv[0]).is_absolute()

    def test_absent_declaration_is_a_no_op(self) -> None:
        assert parse_declared_shell_commands(None, writable_roots=()) == ()
        assert parse_declared_shell_commands([], writable_roots=()) == ()


def test_shell_metacharacters_are_still_refused_for_declared_commands() -> None:
    """Grants widen the allowlist, never the argv binding."""
    declared = parse_declared_shell_commands([_gog()], writable_roots=())
    for command in (
        "gog gmail search x | tee /tmp/out",
        "gog gmail search x && rm -rf /",
        "gog gmail search $(whoami)",
    ):
        assert parse_service_shell_argv(
            command, "maintenance", declared=declared,
        ) is None, command


class TestSchedulerWiring:
    """A scheduler.yaml job's grants must reach its own principal."""

    def test_job_carries_declarations_from_yaml(self) -> None:
        from mimir.scheduler import load_jobs_from_text

        jobs = load_jobs_from_text(
            "- name: morning-briefing\n"
            "  prompt_file: morning-briefing.md\n"
            "  cron: 0 8 * * *\n"
            "  shell_commands:\n"
            "    - exec: gog\n"
            "      path: /bin/echo\n"
            "      subcommands: [[gmail, search]]\n"
        )
        assert jobs[0].shell_commands == [
            {"exec": "gog", "path": "/bin/echo", "subcommands": [["gmail", "search"]]},
        ]

    def test_a_job_without_declarations_is_unchanged(self) -> None:
        from mimir.scheduler import load_jobs_from_text

        jobs = load_jobs_from_text("- name: reflect\n  prompt_file: r.md\n  cron: 0 6 * * 0\n")
        assert jobs[0].shell_commands is None


class TestPollerWiring:
    def test_manifest_may_declare_shell_commands(self, tmp_path: Path) -> None:
        from mimir.pollers import _parse_poller_authority

        state = tmp_path / "state" / "pollers" / "demo"
        state.mkdir(parents=True)
        principal = _parse_poller_authority(
            {
                "profile": "custom",
                "tier": "code-execution",
                "capabilities": ["shell_exec", "bash_jobs_list", "bash_job_output"],
                "scoped_roots": ["state"],
                "shell_commands": [
                    {"exec": "gog", "path": "/bin/echo",
                     "subcommands": [["gmail", "search"]]},
                ],
            },
            name="demo",
            persist_dir=state,
            state_root=tmp_path / "state" / "pollers",
            manifest_path=tmp_path / "skills" / "demo" / "pollers.json",
        )
        assert [d.executable for d in principal.declared_shell_commands] == ["gog"]

    def test_shell_commands_without_shell_exec_is_refused(self, tmp_path: Path) -> None:
        """The capability gate runs first; grants without it would be inert."""
        from mimir.pollers import _parse_poller_authority

        state = tmp_path / "state" / "pollers" / "demo"
        state.mkdir(parents=True)
        with pytest.raises(ValueError, match="without the shell_exec capability"):
            _parse_poller_authority(
                {
                    "profile": "research",
                    "tier": "scoped-with-provenance",
                    "capabilities": ["read_file"],
                    "scoped_roots": ["state"],
                    "shell_commands": [
                        {"exec": "gog", "path": "/bin/echo",
                         "subcommands": [["gmail", "search"]]},
                    ],
                },
                name="demo",
                persist_dir=state,
                state_root=tmp_path / "state" / "pollers",
                manifest_path=tmp_path / "skills" / "demo" / "pollers.json",
            )

    def test_existing_manifests_without_the_key_still_register(self, tmp_path: Path) -> None:
        """shell_commands is optional; the required-key check must not demand it.

        The validator used to compare the authority block against an exact key
        set. Adding a required key that way would have unregistered every poller
        already deployed.
        """
        from mimir.pollers import _parse_poller_authority

        state = tmp_path / "state" / "pollers" / "demo"
        state.mkdir(parents=True)
        principal = _parse_poller_authority(
            {
                "profile": "research",
                "tier": "scoped-with-provenance",
                "capabilities": ["read_file"],
                "scoped_roots": ["state"],
            },
            name="demo",
            persist_dir=state,
            state_root=tmp_path / "state" / "pollers",
            manifest_path=tmp_path / "skills" / "demo" / "pollers.json",
        )
        assert principal.declared_shell_commands == ()


class TestReviewFindings:
    """Regressions for the privilege-escalation findings on PR #1402."""

    def test_executable_inside_a_writable_root_is_refused(self, home: Path) -> None:
        """Finding 1. The pin is only a pin if the agent cannot replace the file.

        The script rule was applied to ``script`` but not to ``path``, so a CLI
        declared under an agent-writable location could be rewritten and then run
        through an otherwise-admitted command shape.
        """
        fake = home / "scratch" / "gog"
        fake.write_text("#!/bin/sh\necho hi\n")
        fake.chmod(0o755)
        with pytest.raises(ValueError, match="agent-writable root"):
            parse_declared_shell_commands(
                [{"exec": "gog", "path": str(fake), "subcommands": [["gmail", "search"]]}],
                writable_roots=agent_writable_roots(home),
            )

    def test_executable_symlink_into_a_writable_root_is_refused(self, home: Path) -> None:
        target = home / "scratch" / "gog"
        target.write_text("#!/bin/sh\n")
        target.chmod(0o755)
        link = home / "scripts" / "gog"
        link.symlink_to(target)
        with pytest.raises(ValueError, match="agent-writable root"):
            parse_declared_shell_commands(
                [{"exec": "gog", "path": str(link), "subcommands": [["gmail", "search"]]}],
                writable_roots=agent_writable_roots(home),
            )

    def test_path_must_be_a_regular_executable_file(self, home: Path) -> None:
        with pytest.raises(ValueError, match="not a regular file"):
            parse_declared_shell_commands(
                [{"exec": "gog", "path": str(home / "scripts"),
                  "subcommands": [["gmail", "search"]]}],
                writable_roots=(),
            )

    def test_interpreter_cannot_be_hidden_behind_an_exec_alias(self, home: Path) -> None:
        """Finding 2. Classification follows the resolved binary, not the label.

        ``exec`` and ``path`` need not share a name, so classifying on ``exec``
        let ``exec: gog`` with ``path: /usr/bin/python3`` present as an ordinary
        CLI and skip the pinned-script rule entirely.
        """
        with pytest.raises(ValueError, match="only be declared with a pinned"):
            parse_declared_shell_commands(
                [{"exec": "gog", "path": "/usr/bin/python3",
                  "subcommands": [["/tmp/whatever.py"]]}],
                writable_roots=(),
            )

    def test_scheduler_declaration_does_not_widen_unrelated_authority(self) -> None:
        """Finding 3. A grant adds the declared argv and nothing else.

        Rebuilding the principal from the heartbeat profile would have handed any
        job that declared one command the full heartbeat capability set --
        send_message, fetch_url, and the typed repo/PR mutation tools.
        """
        import dataclasses

        from mimir.access_control import get_service_principal

        base = get_service_principal("scheduled_tick")
        assert base is not None
        declared = parse_declared_shell_commands(
            [{"exec": "gog", "path": "/bin/echo", "subcommands": [["gmail", "search"]]}],
            writable_roots=(),
        )
        extended = dataclasses.replace(base, declared_shell_commands=declared)

        assert extended.capabilities == base.capabilities
        assert extended.sink_policies == base.sink_policies
        assert extended.readable_domains == base.readable_domains
        assert extended.filesystem_read_roots == base.filesystem_read_roots
        assert extended.capability_tier == base.capability_tier
        assert extended.declared_shell_commands != base.declared_shell_commands

    def test_scheduler_yaml_round_trip_preserves_declarations(self) -> None:
        """Finding 4. Any mutation rewrites the file; unemitted grants vanish."""
        from mimir.scheduler import load_jobs_from_text
        import yaml

        text = (
            "- name: morning-briefing\n"
            "  prompt_file: morning-briefing.md\n"
            "  cron: 0 8 * * *\n"
            "  shell_commands:\n"
            "    - exec: gog\n"
            "      path: /bin/echo\n"
            "      subcommands: [[gmail, search]]\n"
        )
        job = load_jobs_from_text(text)[0]
        round_tripped = load_jobs_from_text(yaml.safe_dump([job.to_yaml_entry()]))[0]
        assert round_tripped.shell_commands == job.shell_commands

    @pytest.mark.parametrize(
        "profile",
        ["maintenance", "repo_review", "scheduler_read_only", "upgrade_workspace"],
    )
    def test_declarations_are_additive_under_every_profile(self, profile: str) -> None:
        """Finding 5. Several branches return early; the check must precede them.

        ``repo_review`` and the per-profile ``git`` handlers return on mismatch,
        so a fallback placed after the dispatch was reachable for some profiles
        and not others -- a GitHub poller could never use a declared CLI.
        """
        declared = parse_declared_shell_commands(
            [{"exec": "gog", "path": "/bin/echo", "subcommands": [["gmail", "search"]]}],
            writable_roots=(),
        )
        assert parse_service_shell_argv(
            "gog gmail search x", profile, declared=declared,
        ) is not None

    def test_ambient_home_does_not_make_scripts_undeclarable(
        self, home: Path, monkeypatch,
    ) -> None:
        """The operator-owned <home>/scripts/ must stay declarable.

        ``_configured_file_write_roots()`` leads with MIMIR_HOME itself, so
        unioning it whole classified everything under home as agent-writable --
        including the one location the interpreter rule documents as the place a
        pinned script should live. The earlier success test missed it because its
        fixture home differed from the ambient MIMIR_HOME; this one sets them
        equal, which is the real deployment shape.
        """
        monkeypatch.setenv("MIMIR_HOME", str(home))
        roots = agent_writable_roots(home)
        assert home.resolve() not in roots, "the home sink root is not itself writable"
        assert (home / "scratch").resolve() in roots, "but its rw folders are"

        declared = parse_declared_shell_commands(
            [{"exec": "python3", "path": "/usr/bin/python3",
              "script": str(home / "scripts" / "todo.py")}],
            writable_roots=roots,
        )
        assert declared[0].script == (home / "scripts" / "todo.py").resolve()

    def test_symlinked_home_does_not_make_scripts_undeclarable(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        real_home = tmp_path / "real-home"
        for name in ("scripts", "scratch", "skills", "state"):
            (real_home / name).mkdir(parents=True)
        script = real_home / "scripts" / "todo.py"
        script.write_text("print(1)\n")
        home = tmp_path / "home"
        home.symlink_to(real_home, target_is_directory=True)
        monkeypatch.setenv("MIMIR_HOME", str(home))

        roots = agent_writable_roots(home)
        assert home.resolve() not in roots, "the resolved home sink root is excluded"
        assert (home / "scratch").resolve() in roots, "but its rw folders are"

        declared = parse_declared_shell_commands(
            [{"exec": "python3", "path": "/usr/bin/python3",
              "script": str(home / "scripts" / "todo.py")}],
            writable_roots=roots,
        )
        assert declared[0].script == script.resolve()

    def test_external_rw_roots_count_as_writable(
        self, home: Path, tmp_path_factory, monkeypatch,
    ) -> None:
        """Finding 1b. MIMIR_FILE_TOOL_ROOTS rw routes are part of the surface.

        The root must sit OUTSIDE the home -- an overlapping entry is rejected by
        config itself, so an in-home directory would not exercise this at all.
        """
        external = tmp_path_factory.mktemp("external_repo")
        binary = external / "gog"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        monkeypatch.setenv("MIMIR_HOME", str(home))
        monkeypatch.setenv("MIMIR_FILE_TOOL_ROOTS", f"{external}:rw")
        roots = agent_writable_roots(home)
        assert external.resolve() in roots, roots
        with pytest.raises(ValueError, match="agent-writable root"):
            parse_declared_shell_commands(
                [{"exec": "gog", "path": str(binary), "subcommands": [["gmail", "search"]]}],
                writable_roots=roots,
            )


@pytest.mark.parametrize("raw", [{}, "", 0, False])
def test_malformed_falsey_scheduler_declaration_is_not_silently_ignored(raw) -> None:
    """A falsey malformed shape must reach validation, not skip it.

    `Scheduler._fire` branched on truthiness, so `shell_commands: {}` or `: ""`
    stored fine, skipped validation entirely, and the job fired with no
    declaration and no error — silent misconfiguration, which is the failure
    this feature exists to prevent. Found by documenting the contract.
    """
    with pytest.raises(ValueError, match="must be a list"):
        parse_declared_shell_commands(raw, writable_roots=())


def test_an_explicit_empty_list_is_valid_and_means_nothing_declared() -> None:
    """Distinct from malformed: `shell_commands: []` is a legitimate no-op."""
    assert parse_declared_shell_commands([], writable_roots=()) == ()
    assert parse_declared_shell_commands(None, writable_roots=()) == ()
