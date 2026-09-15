from __future__ import annotations

import runpy
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"
SERVICE = ROOT / "deploy/s6-overlay/s6-rc.d/worklink-execd"


def test_image_declares_distinct_fixed_controller_and_worker_identities() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "groupadd --gid 1001 mimir" in text
    assert "groupadd --gid 1002 worklink" in text
    assert "--uid 1001 --gid mimir --groups worklink" in text
    assert "--uid 1002 --gid worklink" in text
    assert "chmod 0700 /home/mimir" in text


def test_image_provisions_protected_worklink_roots() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "install -d -o root -g mimir -m 0771 /var/lib/mimir-worklink/checkouts" in text
    assert "install -d -o root -g mimir -m 0771 /var/lib/mimir-worklink/repo-test-checkouts" in text
    assert "install -d -o root -g mimir -m 0771 /var/lib/mimir-worklink/opencode-checkouts" in text
    assert "install -d -o root -g worklink -m 0710 /var/lib/mimir-worklink/homes" in text


def test_root_executor_is_immutable_and_installed_outside_user_homes() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert 'ARG MIMIR_GIT_REF' in text
    assert 'ARG MIMIR_EXECUTOR_COMMIT' in text
    assert "grep -Eq '^[0-9a-f]{40}$'" in text
    assert 'git check-ref-format "$MIMIR_GIT_REF"' in text
    # The pinned commit must be fetched BEFORE the caller's ref. Fetching only the
    # ref races every merge: it moves between build start and fetch, FETCH_HEAD
    # resolves to a newer commit, and the SHA guard below fails a build whose own
    # checks were green. Asserted on whitespace-normalised text so the Dockerfile
    # may wrap these lines, and by ORDER so reintroducing the race fails here.
    # Join shell line-continuations first, then collapse whitespace, so a wrapped
    # command reads as the single command the shell actually executes.
    flat = " ".join(text.replace("\\\n", " ").split())
    sha_fetch = 'fetch --no-tags --depth=1 origin "$MIMIR_EXECUTOR_COMMIT"'
    ref_fetch = 'fetch --no-tags --depth=1 origin "$MIMIR_GIT_REF"'
    assert sha_fetch in flat, "executor source must be fetched by immutable commit"
    assert ref_fetch in flat, "ref fetch must remain as the reachability fallback"
    assert flat.index(sha_fetch) < flat.index(ref_fetch), (
        "the immutable commit must be fetched first; fetching the moving ref first "
        "reintroduces the merge race that reddened main on 19d7c517"
    )
    # Ordering alone is not the invariant. If the `||` became `&&` -- or the ref
    # fetch simply ran afterwards unconditionally -- the moving ref would overwrite
    # FETCH_HEAD and restore the race while presence and ordering both still held.
    # Pin the operator, so the contract distinguishes a FALLBACK from a later
    # unconditional fetch.
    between = flat[flat.index(sha_fetch) + len(sha_fetch):flat.index(ref_fetch)]
    assert between.lstrip().startswith("||"), (
        "the ref fetch must be a FALLBACK (`||`) for the immutable-SHA fetch, not an "
        "unconditional fetch after it. Anything that runs the moving-ref fetch when "
        "the SHA fetch already succeeded overwrites FETCH_HEAD and restores the race; "
        f"found {between.strip()[:40]!r} between them"
    )
    # The SHA stays authoritative whichever fetch succeeded.
    assert 'rev-parse FETCH_HEAD' in text
    assert 'git -C /opt/mimir-worklink/source checkout --detach FETCH_HEAD' in text
    assert 'git -C /opt/mimir-worklink/source status --porcelain=v1' in text
    assert 'executor-source-commit' in text
    assert "COPY --chown=root:root mimir/ /opt/mimir-worklink/source/mimir/" not in text
    # TRANSITIONAL, and deliberately version-agnostic. The worker venv takes its
    # dependency set from the published wheel and then overlays this checkout with
    # --no-deps, so a runtime dependency newer than the last release is absent and
    # the overlay cannot import it. Delete this assertion and the Dockerfile line it
    # guards once a published wheel declares pypdf. Asserting the exact specifier
    # here would make a floor bump fail an unrelated image-identity test.
    assert "/opt/mimir-worklink/venv/bin/pip install --no-cache-dir" in text
    assert "pypdf" in text
    assert "pip install --no-cache-dir --no-deps /opt/mimir-worklink/source" in text
    assert "UV_CACHE_DIR=/opt/mimir-worklink/uv-cache uv sync" in text
    assert "--locked --extra dev --extra bench --no-install-workspace" in text
    assert "rm -rf /opt/mimir-worklink/source/.venv" in text
    assert "rm -rf /opt/mimir-worklink/source" in text
    assert "chmod 0755 /usr/local/libexec/worklink-execd" in text
    assert "PYTHONPATH=" not in text
    assert "/opt/mimir-worklink/venv/bin/python -m mimir.worklink.worker_exec" in text
    assert "chown -R root:root /opt/mimir-worklink" in text
    assert "chmod -R go-w /opt/mimir-worklink" in text


def test_executor_build_refuses_controller_commit_mismatch() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert 'test "$MIMIR_EXECUTOR_COMMIT" = "$MIMIR_CONTROLLER_COMMIT"' in text


def test_s6_registers_one_root_executor_service() -> None:
    run = (SERVICE / "run").read_text(encoding="utf-8")
    assert (SERVICE / "type").read_text(encoding="utf-8") == "longrun\n"
    assert (SERVICE / "dependencies.d/base").is_file()
    assert (ROOT / "deploy/s6-overlay/s6-rc.d/user/contents.d/worklink-execd").is_file()
    assert "s6-setuidgid" not in run
    assert run.rstrip().endswith("exec /usr/local/libexec/worklink-execd")


def test_executor_service_recreates_ephemeral_socket_layout() -> None:
    run = (SERVICE / "run").read_text(encoding="utf-8")
    assert "install -d -o root -g root -m 0711 /run/mimir-worklink" in run
    assert "install -d -o root -g mimir -m 0710 /run/mimir-worklink/socket" in run


def test_spawn_image_proof_uses_one_checked_seed_tree() -> None:
    namespace = runpy.run_path(ROOT / "scripts/worklink_image_identity.py")
    spawn_proof = namespace["SPAWN_PROOF"]

    compile(spawn_proof, "SPAWN_PROOF", "exec")
    assert '"default_cwd": SEED' in spawn_proof
    assert '["git", "-C", str(SEED), "status", "--porcelain=v1", "-z"]' in spawn_proof
    assert "def seed_status() -> bytes:" in spawn_proof
    assert "check=True" in spawn_proof
    assert "/home/mimir/worklink-source" not in spawn_proof


def test_image_proof_passes_the_github_remote_ref(monkeypatch) -> None:
    namespace = runpy.run_path(ROOT / "scripts/worklink_image_identity.py")
    monkeypatch.delenv("MIMIR_GIT_REF", raising=False)
    monkeypatch.setenv("GITHUB_REF", "refs/pull/1755/merge")

    assert namespace["source_ref"]() == "refs/pull/1755/merge"
    assert 'f"MIMIR_GIT_REF={git_ref}"' in (ROOT / "scripts/worklink_image_identity.py").read_text()


def _worker_uid_job() -> dict:
    """Return the parsed ``pytest-worker-uid`` job.

    Parsed rather than substring-matched against the raw file: every property
    asserted below is also *described* in that job's comments, so a raw-text
    assertion would still pass after the step it describes was deleted. YAML
    parsing drops comments, so these assertions can only be satisfied by the
    executable steps.
    """
    import yaml

    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
    )
    job = workflow["jobs"].get("pytest-worker-uid")
    assert job is not None, "the non-owning-uid CI leg is gone"
    return job


def test_ci_runs_the_suite_as_a_uid_that_owns_neither_checkout_nor_home() -> None:
    """The leg must actually run the suite as a non-owning uid.

    Reduced to a plain `pytest` invocation it becomes a fourth identical
    owner-uid run: green, expensive, and blind to the class it exists for.
    """
    job = _worker_uid_job()
    runs = " ".join(step.get("run", "") for step in job["steps"])

    assert "sudo -u worklink env" in runs
    assert "HOME=/nonexistent" in runs
    assert ".venv/bin/python -m pytest" in runs
    # `uv run` would derive its cache from HOME, which is deliberately unwritable.
    assert "uv run pytest" not in runs

    # The worker must be able to read the tree and must NOT be able to write it.
    assert "chmod -R g+rX ." in runs
    assert "g+w" not in runs
    assert "sudo -u worklink test -r" in runs
    assert "sudo -u worklink test -w" in runs


def test_ci_worker_uid_leg_seeds_the_state_that_makes_it_discriminating() -> None:
    """Pin the seeding, which is what makes this leg catch anything.

    These failures need the controller's state to EXIST and be unreadable, not
    to be absent: Config handles a missing credentials file gracefully, and an
    unset MIMIR_FILE_TOOL_ROOTS reproduces nothing. Deleting either seed leaves
    the leg green against currently-fixed code while silently reducing it to a
    vacuous owner-independent run, and the job cannot detect that about itself.

    Reverting PR #1760 took this leg from 0 to 42 failures while every other leg
    stayed green; that margin is what these assertions protect.
    """
    job = _worker_uid_job()
    runs = " ".join(step.get("run", "") for step in job["steps"])

    # The credentials file must exist, and be unreadable to the worker uid --
    # mode 600 inside a mode 700 directory owned by the runner.
    assert ".claude/.credentials.json" in runs
    assert 'chmod 700 "$HOME/.claude"' in runs
    assert 'chmod 600 "$HOME/.claude/.credentials.json"' in runs

    # Both ambient-state surfaces must reach the pytest process itself.
    assert "MIMIR_CLAUDE_OAUTH_CREDENTIALS=" in runs
    assert "MIMIR_FILE_TOOL_ROOTS=" in runs

    # MIMIR_FILE_TOOL_ROOTS must be set to a real value, not left empty.
    env = next(
        step for step in job["steps"]
        if step.get("name") == "Run mimir test suite as the non-owning worker uid"
    ).get("env") or {}
    assert env.get("MIMIR_FILE_TOOL_ROOTS")


def test_ci_evidence_fixtures_stay_outside_controller_home() -> None:
    """Evidence retention must not move fixtures into controller-owned ancestry."""
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
    )
    assert "PYTEST_EVIDENCE_ROOT" not in workflow["env"]
    root = workflow["env"]["PYTEST_EVIDENCE_DIR_NAME"]
    assert root.startswith("mimir-pytest-evidence-")
    assert "${{ github.run_id }}" in root
    assert "${{ github.run_attempt }}" in root
    evidence_jobs = 0
    for job in workflow["jobs"].values():
        steps = job["steps"]
        uploads = [step for step in steps if step.get("name") == "Upload pytest evidence"]
        if not uploads:
            continue
        evidence_jobs += 1
        runs = "\n".join(step.get("run", "") for step in steps)
        assert '--basetemp "$PYTEST_EVIDENCE_ROOT/' in runs
        assert "$RUNNER_TEMP/pytest-evidence" not in runs
        assert 'echo "PYTEST_EVIDENCE_ROOT=$evidence_root" >> "$GITHUB_ENV"' in runs
        prepare = next(
            (step for step in steps if step.get("name") == "Prepare pytest evidence directory"),
            None,
        )
        if prepare is not None:
            script = prepare["run"]
            assert 'evidence_root="/tmp/$PYTEST_EVIDENCE_DIR_NAME"' in script
            assert 'chgrp "$(id -g)" "$evidence_root"' in script
            assert 'chmod 700 "$evidence_root"' in script
            assert 'evidence_root="$(cd "$evidence_root" && pwd -P)"' in script
            assert script.index("chgrp") < script.index("chmod") < script.index("pwd -P")
            assert steps.index(prepare) < next(
                i for i, step in enumerate(steps) if "--basetemp" in step.get("run", "")
            )
        for upload in uploads:
            assert upload["if"] == "failure()"
            assert not upload.get("continue-on-error", False)
            assert "${{ github.run_id }}" in upload["with"]["name"]
            assert "${{ github.run_attempt }}" in upload["with"]["name"]
            assert upload["with"]["path"] == "${{ steps.stage-pytest-evidence.outputs.path }}"
            stage = next(step for step in steps if step.get("id") == "stage-pytest-evidence")
            assert stage["if"] == "failure()"
            assert steps.index(stage) < steps.index(upload)
            assert "os.walk(root, followlinks=False)" in stage["run"]
            assert "if not stat.S_ISREG(source.lstat().st_mode):" in stage["run"]
            assert 'quote(part, safe="", errors="surrogatepass")' in stage["run"]
            assert "safe_component(part) for part in source.relative_to(root).parts" in stage["run"]
            assert "max_files = 5000" in stage["run"]
            assert "max_bytes = 100 * 1024 * 1024" in stage["run"]
            assert "max_file_bytes = 5 * 1024 * 1024" in stage["run"]
            paths = stage["env"]["PYTEST_EVIDENCE_PATHS"].splitlines()
            assert paths
            assert all(path.startswith("${{ env.PYTEST_EVIDENCE_ROOT }}/") for path in paths)
            for suffix in ("", ".wakeup", ".diagnostics", ".stacks"):
                assert "${{ env.PYTEST_EVIDENCE_ROOT }}/**/child-progress" + suffix in paths
    assert evidence_jobs == 7
    worker_runs = "\n".join(step.get("run", "") for step in _worker_uid_job()["steps"])
    assert 'sudo install -d -m 700 -o worklink -g worklink "$evidence_root"' in worker_runs
    assert 'evidence_root="$(cd /tmp && pwd -P)/$PYTEST_EVIDENCE_DIR_NAME"' in worker_runs
    assert 'sudo chmod o+x "$RUNNER_TEMP"' not in worker_runs


def test_ci_evidence_staging_skips_symlinks(tmp_path) -> None:
    """Run the workflow collector against pathological fixtures, not a reimplementation."""
    import json
    import os
    import shutil
    import subprocess
    from urllib.parse import unquote

    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
    )
    stage = next(
        step for step in workflow["jobs"]["skill-conformance"]["steps"]
        if step.get("id") == "stage-pytest-evidence"
    )
    root = tmp_path / "evidence"
    fixture = root / "main" / "popen-gw0" / "fixture"
    fixture.mkdir(parents=True)
    expected = {}
    for pattern in stage["env"]["PYTEST_EVIDENCE_PATHS"].splitlines():
        name = Path(pattern).name.replace("*", "child")
        source = fixture / name
        source.write_text(name)
        expected[str(source.relative_to(root))] = name
    (fixture / "irrelevant.txt").write_text("not evidence")
    # Include literal escapes, hidden names and expansion beyond NAME_MAX too.
    for value in ['"', ':', '<', '>', '|', '*', '?', '\r', '\n', '%3A', '\\', '+', '.hidden', ':' * 240]:
        directory = fixture / value
        directory.mkdir()
        source = directory / (value + ".stdout.log")
        source.write_text(repr(value))
        expected[str(source.relative_to(root))] = repr(value)
    # Many modest components also overflow the total path after encoding.
    deep = fixture.joinpath(*([":" * 18] * 12))
    deep.mkdir(parents=True)
    deep_source = deep / ((":" * 100) + ".stdout.log")
    deep_source.write_text("deep evidence")
    expected[str(deep_source.relative_to(root))] = "deep evidence"
    # Literal fallback-looking source directories remain in a distinct namespace.
    literal = root / "+long" / "child-progress"
    literal.parent.mkdir()
    literal.write_text("literal namespace")
    expected[str(literal.relative_to(root))] = "literal namespace"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "child-progress").write_text("outside evidence")
    (fixture / "loop").symlink_to(fixture / "loop")
    (fixture / "directory-loop").symlink_to(root, target_is_directory=True)
    (fixture / "outside").symlink_to(outside, target_is_directory=True)
    for name, target in (
        ("stdout.log", fixture / "link-stdout.log" / "stdout.log"),
        ("stderr.log", tmp_path / "missing"),
        ("child-progress", outside / "child-progress"),
    ):
        link_dir = fixture / ("link-" + name)
        link_dir.mkdir()
        (link_dir / name).symlink_to(target)
    output = tmp_path / "github-output"
    subprocess.run(
        ["bash", "-c", stage["run"]], check=True, capture_output=True, text=True,
        env={**os.environ, "PYTEST_EVIDENCE_ROOT": str(root),
             "PYTEST_EVIDENCE_PATHS": stage["env"]["PYTEST_EVIDENCE_PATHS"],
             "GITHUB_OUTPUT": str(output)},
    )
    staged = Path(output.read_text().strip().removeprefix("path="))
    try:
        assert not any(path.is_symlink() for path in staged.rglob("*"))
        summary = (staged / "STAGING.txt").read_text()
        mappings = json.loads(summary.splitlines()[-1])
        long_original = str((fixture / (":" * 240) / ((":" * 240) + ".stdout.log")).relative_to(root))
        decoded_mappings = {key: unquote(value, errors="surrogatepass")
                            for key, value in mappings.items()}
        assert long_original in decoded_mappings.values()
        assert str(deep_source.relative_to(root)) in decoded_mappings.values()
        assert len(decoded_mappings) >= 2
        for key, original in decoded_mappings.items():
            assert key.startswith("+long/")
            assert (staged / key).read_text() == expected[original]
        for path in staged.rglob("*"):
            # Platform-independent regression: Linux must enforce the macOS bound too.
            assert len(os.fsencode(path.resolve())) <= 900
            assert all(len(os.fsencode(part)) <= 255 for part in path.parts)
            relative = str(path.relative_to(staged))
            assert not any(char in relative for char in '\":<>|*?\r\n\\')
            assert not any(part.startswith(".") for part in path.relative_to(staged).parts)
        assert {
            decoded_mappings.get(str(path.relative_to(staged)),
                                 unquote(str(path.relative_to(staged)).replace("+/", ""),
                                         errors="surrogatepass")): path.read_text()
            for path in staged.rglob("*") if path.is_file() and path.name != "STAGING.txt"
        } == expected
        assert "truncated files: 0; stopped before remaining evidence: False" in (staged / "STAGING.txt").read_text()
    finally:
        shutil.rmtree(staged)


@pytest.mark.parametrize(
    "file_limit,byte_limit,per_file_limit,expected,truncated",
    [(2, 100, 10, [b"abcdef", b"abcdef"], 0),
     (10, 8, 10, [b"abcdef", b"ab"], 1),
     (10, 100, 2, [b"ab"] * 3, 3)],
)
def test_ci_evidence_staging_bounds(tmp_path, file_limit, byte_limit, per_file_limit, expected, truncated) -> None:
    import os
    import shutil
    import subprocess

    workflow = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text())
    stage = next(step for step in workflow["jobs"]["skill-conformance"]["steps"]
                 if step.get("id") == "stage-pytest-evidence")
    # Exercise the real collector with small budgets, not multi-megabyte fixtures.
    script = stage["run"].replace("max_files = 5000", f"max_files = {file_limit}")
    script = script.replace("max_bytes = 100 * 1024 * 1024", f"max_bytes = {byte_limit}")
    script = script.replace("max_file_bytes = 5 * 1024 * 1024", f"max_file_bytes = {per_file_limit}")
    root = tmp_path / "evidence"
    root.mkdir()
    for index in reversed(range(3)):
        (root / f"{index}.stdout.log").write_bytes(b"abcdef")
    output = tmp_path / "github-output"
    subprocess.run(
        ["bash", "-c", script], check=True, capture_output=True, text=True,
        env={**os.environ, "PYTEST_EVIDENCE_ROOT": str(root),
             "PYTEST_EVIDENCE_PATHS": stage["env"]["PYTEST_EVIDENCE_PATHS"],
             "GITHUB_OUTPUT": str(output)},
    )
    staged = Path(output.read_text().strip().removeprefix("path="))
    try:
        assert [path.read_bytes() for path in sorted(staged.iterdir())
                if path.name != "STAGING.txt"] == expected
        summary = (staged / "STAGING.txt").read_text()
        assert f"truncated files: {truncated}" in summary
        assert f"stopped before remaining evidence: {len(expected) < 3}" in summary
        assert len(list(staged.iterdir())) == len(expected) + 1
    finally:
        shutil.rmtree(staged)


def test_ci_evidence_prepare_exports_physical_private_member_group_root(tmp_path) -> None:
    """Execute the actual prepare script with both real and symlinked temp roots."""
    import os
    import stat
    import subprocess

    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
    )
    physical = tmp_path / "physical"
    physical.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(physical, target_is_directory=True)
    for job_name in ("pytest", "pytest-macos"):
        script = next(
            step["run"] for step in workflow["jobs"][job_name]["steps"]
            if step.get("name") == "Prepare pytest evidence directory"
        )
        for temp_root in (physical, alias):
            name = f"mimir-pytest-evidence-{job_name}-{temp_root.name}"
            env_file = tmp_path / f"env-{name}"
            env_file.touch()
            subprocess.run(
                ["bash", "-c", script.replace('"/tmp/', f'"{temp_root}/')],
                env={**os.environ, "PYTEST_EVIDENCE_DIR_NAME": name, "GITHUB_ENV": str(env_file)},
                check=True, capture_output=True, text=True,
            )
            exported = env_file.read_text().strip().removeprefix("PYTEST_EVIDENCE_ROOT=")
            root = physical / name
            assert exported == str(root.resolve())
            assert root.stat().st_uid == os.getuid()
            assert root.stat().st_gid == os.getgid()
            assert stat.S_IMODE(root.stat().st_mode) == 0o700
            assert (root / "optional-skills").is_dir()
            boundary = root / "boundary"
            boundary.mkdir()
            boundary.chmod(0o2700)
            assert stat.S_IMODE(boundary.stat().st_mode) == 0o2700


def test_ci_frontend_caches_root_dependencies_and_bounds_build() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
    )
    job = workflow["jobs"]["frontend"]
    assert job["timeout-minutes"] == 15
    steps = job["steps"]
    setup = next(step for step in steps if step.get("uses", "").startswith("actions/setup-node@"))
    assert setup["with"]["cache"] == "npm"
    cache = next(step for step in steps if step.get("uses", "").startswith("actions/cache@"))
    assert cache["with"]["path"] == "node_modules"
    assert "${{ runner.os }}" in cache["with"]["key"]
    # Use the resolved Node version, so changing setup-node invalidates the tree.
    assert setup.get("id")
    node_version = "${{ steps." + setup["id"] + ".outputs.node-version }}"
    assert node_version in cache["with"]["key"]
    assert "${{ hashFiles('package-lock.json') }}" in cache["with"]["key"]
    assert not cache["with"].get("restore-keys")
    install = next(step for step in steps if step.get("run") == "npm ci")
    assert install["if"] == f"steps.{cache['id']}.outputs.cache-hit != 'true'"
    build = next(step for step in steps if step.get("name") == "Typecheck and build React app")
    assert build["timeout-minutes"] == 5
    assert build["run"].splitlines() == ["npm run test", "npm run build"]
    assert steps.index(setup) < steps.index(cache) < steps.index(install) < steps.index(build)


def test_ci_runs_the_committed_live_image_proof() -> None:
    workflow = (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
    proof = (ROOT / "scripts/worklink_image_identity.py").read_text(encoding="utf-8")
    assert "worklink-image-identity:" in workflow
    assert "uv run python scripts/worklink_image_identity.py" in workflow
    assert 'stat -c %U:%G /opt/mimir-worklink/uv-cache' in proof
    assert 'stat -c %a /opt/mimir-worklink/uv-cache' in proof
    assert "sibling-access negative control did not detect a cross-write" in proof
    assert "intentionally shared sibling checkout" in proof
    parsed = yaml.safe_load(workflow)
    configured_repo = parsed["jobs"]["worklink-image-identity"]["env"]["WORKLINK_REPO"]
    assert configured_repo == "/workspace/worklink-base"
    assert 'REPO = Path(os.environ["WORKLINK_REPO"])' in proof
    assert 'SEED = Path(os.environ["WORKLINK_REPO"])' in proof
    assert 'repo = Path(Path("/tmp/worklink-proof-repo").read_text())' in proof
    assert 'shlex.quote(str(repo / \'tracked\'))' in proof
    assert "/workspace/mimir" not in proof
    assert "worklink-publication-attack" in proof
    assert 'remote", "set-url", "--push"' in proof
    assert "ControllerGitPublication.capture" in proof
    assert "Worklink unexpectedly selected the contained checkout path" in proof
    assert "issue_id=1411" in proof
