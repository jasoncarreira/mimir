from __future__ import annotations

import runpy
from pathlib import Path

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
    env = job["steps"][-1].get("env") or {}
    assert env.get("MIMIR_FILE_TOOL_ROOTS")


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
