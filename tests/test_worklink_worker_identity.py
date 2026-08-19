from __future__ import annotations

from pathlib import Path


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
    assert "COPY --chown=root:root mimir/ /opt/mimir-worklink/source/mimir/" in text
    # TRANSITIONAL, and deliberately version-agnostic. The worker venv takes its
    # dependency set from the published wheel and then overlays this checkout with
    # --no-deps, so a runtime dependency newer than the last release is absent and
    # the overlay cannot import it. Delete this assertion and the Dockerfile line it
    # guards once a published wheel declares pypdf. Asserting the exact specifier
    # here would make a floor bump fail an unrelated image-identity test.
    assert "/opt/mimir-worklink/venv/bin/pip install --no-cache-dir" in text
    assert "pypdf" in text
    assert "pip install --no-cache-dir --no-deps /opt/mimir-worklink/source" in text
    assert "rm -rf /opt/mimir-worklink/source" in text
    assert "chmod 0755 /usr/local/libexec/worklink-execd" in text
    assert "PYTHONPATH=" not in text
    assert "/opt/mimir-worklink/venv/bin/python -m mimir.worklink.worker_exec" in text
    assert "chown -R root:root /opt/mimir-worklink" in text
    assert "chmod -R go-w /opt/mimir-worklink" in text


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


def test_ci_runs_the_committed_live_image_proof() -> None:
    workflow = (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
    proof = (ROOT / "scripts/worklink_image_identity.py").read_text(encoding="utf-8")
    assert "worklink-image-identity:" in workflow
    assert "uv run python scripts/worklink_image_identity.py" in workflow
    assert "sibling-access negative control did not detect a cross-write" in proof
    assert "worker reached concurrent sibling checkout" in proof
    assert "issue_id=1411" in proof
