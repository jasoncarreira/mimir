from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
IMAGE = f"mimir-worklink-identity:{os.getpid()}"
CONTAINER = f"mimir-worklink-identity-{os.getpid()}"


def run(args: list[str], *, input_text: str | None = None, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args, cwd=ROOT, input=input_text, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, timeout=timeout, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"command failed ({result.returncode}): {shlex.join(args)}\n{result.stdout}")
    return result


def docker_exec(*args: str, user: str = "root", input_text: str | None = None) -> str:
    command = ["docker", "exec", "--user", user]
    if input_text is not None:
        command.append("-i")
    command.extend([CONTAINER, *args])
    return run(command, input_text=input_text).stdout


def wait_for_runtime() -> None:
    deadline = time.monotonic() + 45
    last = ""
    while time.monotonic() < deadline:
        probe = subprocess.run(
            ["docker", "exec", CONTAINER, "/bin/sh", "-c",
             "test -S /run/mimir-worklink/socket/worklink-execd.sock"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
        )
        last = probe.stdout
        if probe.returncode == 0:
            return
        time.sleep(1)
    logs = run(["docker", "logs", CONTAINER]).stdout
    raise RuntimeError(f"shipped services did not become ready\n{last}\n{logs}")


CONTROLLER_PROOF = r"""
from __future__ import annotations
import asyncio
from datetime import UTC, datetime
import json
import os
from pathlib import Path

from mimir.worklink.backends.base import WorkOrder
from mimir.worklink.backends.opencode import OpenCodeBackend
from mimir.worklink.checkout import cleanup_checkout, create_isolated_checkout
from mimir.worklink.compute import LocalSubprocessComputeBackend
from mimir.worklink.evidence import observe_evidence
from mimir.worklink.safe_git import ControllerGitPublication

REPO = Path("/home/mimir/worklink-source")
METADATA = Path("/home/mimir/worklink-publication")
CONFIG = Path("/home/mimir/worklink-opencode/opencode.json")
DATA = Path("/home/mimir/worklink-opencode/data")
CLI = "/tmp/opencode-proof"

async def main():
    if os.geteuid() != 1001:
        raise RuntimeError("controller proof is not running as mimir")
    os.environ.update({
        "MIMIR_CODING_ENABLED": "true",
        "MIMIR_MODEL_SPEC": "proof:model",
        "OPENCODE_CONFIG": str(CONFIG),
        "XDG_DATA_HOME": str(DATA),
    })
    lease = create_isolated_checkout(
        REPO, issue_id=1410, attempt=1, base="main", worker_eligible=True
    )
    sibling = create_isolated_checkout(
        REPO, issue_id=1411, attempt=1, base="main", worker_eligible=True
    )
    if not lease.worker_authorized or lease.authorization is None:
        raise RuntimeError("production checkout factory did not authorize worker checkout")
    if not sibling.worker_authorized or sibling.authorization is None:
        raise RuntimeError("concurrent checkout factory did not authorize worker checkout")
    sibling.path.joinpath("sibling-canary").write_text("sibling-original")
    source_object = next(REPO.joinpath(".git/objects").glob("[0-9a-f][0-9a-f]/*"))
    checkout_object = lease.path / ".git/objects" / source_object.relative_to(REPO / ".git/objects")
    if source_object.stat().st_ino == checkout_object.stat().st_ino:
        raise RuntimeError("issued checkout retained a source hardlink")
    checkout_fd = lease.authorization.duplicate_fd()
    try:
        publication = ControllerGitPublication.capture(
            checkout_fd, REPO, lease.branch, METADATA
        )
    finally:
        os.close(checkout_fd)
    compute = LocalSubprocessComputeBackend.for_authorized_checkout(lease.authorization)
    backend = OpenCodeBackend(bin=CLI)
    order = WorkOrder(
        issue_id=1410,
        checkout=lease.path,
        prompt="apply the runtime proof edit",
        rules=None,
        timeout_s=30,
        env={},
    )
    spec = backend.work_spec(
        order,
        attempt=1,
        repo_url=str(REPO),
        base_ref="main",
        branch=lease.branch,
        test_command="unused",
    )
    projections = spec.backend_config.get("worker_projections", ())
    if len(projections) != 2:
        raise RuntimeError("configured provider config and auth were not projected")
    handle = await compute.launch(spec)
    try:
        result = await compute.wait(handle, spec.timeout_s)
    finally:
        await compute.cleanup(handle)
    if result.exit_code != 0 or "build-euid=1002" not in result.stdout:
        raise RuntimeError(f"production build failed: {result}")
    if lease.path.joinpath("tracked").read_text() != "worker-modified":
        raise RuntimeError("controller could not observe worker edit")
    lease.path.joinpath("controller-continuation").write_text("continued")
    gate = await observe_evidence(
        issue=1410,
        attempt=1,
        backend="opencode",
        branch=lease.branch,
        checkout=lease.path,
        started_at=datetime.now(UTC),
        base_ref=lease.local_base,
        backend_status="completed",
        test_command=r'''test "$(id -u)" = 1002
printf 'gate-euid=%s\\n' "$(id -u)"
test "$(cat tracked)" = worker-modified
test "$(cat created)" = worker-created
test "$(cat controller-continuation)" = continued
! test -e deleted
test -r "$HOME/.config/opencode/opencode.json"
test -r "$HOME/.local/share/opencode/auth.json"
! cat /home/mimir/worklink-canary
''',
        safe_git=publication,
        work_spec=spec,
        compute=compute,
    )
    if gate.evidence.tests is None or gate.evidence.tests.exit_code != 0:
        raise RuntimeError(f"production evidence gate failed: {gate}")
    if "gate-euid=1002" not in (gate.evidence.tests.summary or ""):
        raise RuntimeError("controller did not observe evidence-gate worker identity")
    if "tracked" not in gate.evidence.files_changed:
        raise RuntimeError("controller evidence did not observe worker changes")
    if Path("/home/mimir/worklink-canary").read_text() != "controller-reset":
        raise RuntimeError("worker changed controller canary")
    if sibling.path.joinpath("sibling-canary").read_text() != "sibling-original":
        raise RuntimeError("worker changed concurrent sibling checkout")
    cleanup_checkout(lease, outcome="completed", safe_git=publication)
    sibling_fd = sibling.authorization.duplicate_fd()
    try:
        sibling_publication = ControllerGitPublication.capture(
            sibling_fd, REPO, sibling.branch, METADATA / "sibling"
        )
    finally:
        os.close(sibling_fd)
    cleanup_checkout(sibling, outcome="completed", safe_git=sibling_publication)
    sibling_publication.close()
    publication.close()
    if lease.path.exists():
        raise RuntimeError("production checkout cleanup failed")

asyncio.run(main())
"""


def main() -> None:
    run(["docker", "info"], timeout=30)
    service = (ROOT / "deploy/s6-overlay/s6-rc.d/mimir/run").read_text()
    expected = 'exec s6-setuidgid mimir mimir run --home "${MIMIR_HOME:-/home/mimir/agent}"'
    if expected not in service:
        raise RuntimeError("controller s6 service no longer drops to mimir")
    try:
        run(["docker", "build", "--tag", IMAGE, "."], timeout=1200)
        run(["docker", "run", "--detach", "--env", "HOME=/home/mimir", "--name", CONTAINER, IMAGE])
        wait_for_runtime()
        docker_exec("/bin/sh", "-ceu", """
            test "$(id -u mimir)" = 1001
            test "$(id -u worklink)" = 1002
            test "$(stat -c %a:%u:%g /home/mimir)" = 700:1001:1001
            test "$(stat -c %U:%G /usr/local/libexec/worklink-execd)" = root:root
            test "$(stat -c %U:%G /opt/mimir-worklink/venv/bin/python)" = root:root
            test "$(stat -c %a:%u:%g /var/lib/mimir-worklink/homes)" = 710:0:1002
        """)
        # Read the live uids from /proc INSIDE the container.
        #
        # ``docker top`` is unusable for this. It runs ps on the HOST, so its
        # ``user`` column resolves against the host's passwd database: on a
        # GitHub runner host uid 1001 is ``runner``, so a correctly-dropped
        # mimir service reports as ``runner`` and a name comparison fails while
        # the container is entirely correct. Asking that host ps for a numeric
        # ``uid`` column is not portable either — Docker Desktop's ps rejects it
        # outright (``bad -o argument 'uid'``), so the check would then pass on
        # GitHub and fail locally, trading one host dependency for another.
        #
        # /proc needs no ps at all, which also keeps this working in the shipped
        # image, where procps is absent.
        process_table = docker_exec("/bin/sh", "-ceu", r"""
            for proc in /proc/[0-9]*; do
                [ -r "$proc/cmdline" ] || continue
                cmd=$(tr '\0' ' ' < "$proc/cmdline")
                [ -n "$cmd" ] || continue
                uid=$(awk '/^Uid:/ {print $2; exit}' "$proc/status" 2>/dev/null) || continue
                printf '%s\t%s\t%s\n' "${proc#/proc/}" "$uid" "$cmd"
            done
        """)
        rows = [line.split("\t", 2) for line in process_table.splitlines() if line.count("\t") >= 2]
        if not any(uid == "1001" and "mimir run" in cmd for _, uid, cmd in rows):
            raise RuntimeError(f"live mimir service is not uid 1001\n{process_table}")
        if not any(uid == "0" and "mimir.worklink.worker_exec" in cmd for _, uid, cmd in rows):
            raise RuntimeError(f"live worker executor is not root\n{process_table}")

        setup = """
            set -eu
            /command/s6-setuidgid mimir sh -ceu 'printf controller-writable > /home/mimir/worklink-canary; printf controller-reset > /home/mimir/worklink-canary'
            test "$(cat /home/mimir/worklink-canary)" = controller-reset
            /command/s6-setuidgid mimir sh -ceu '
              rm -rf /home/mimir/worklink-source /home/mimir/worklink-remote.git /home/mimir/worklink-opencode /home/mimir/worklink-publication
              git init --bare -q /home/mimir/worklink-remote.git
              git init -q /home/mimir/worklink-source
              cd /home/mimir/worklink-source
              git remote add origin /home/mimir/worklink-remote.git
              printf base > tracked
              printf remove > deleted
              git add tracked deleted
              git -c user.name=test -c user.email=test@example.com commit -qm base
              git branch -M main
              git push -q -u origin main
              git config user.name test
              git config user.email test@example.com
              mkdir -p /home/mimir/worklink-opencode/data/opencode
            '
            cat > /home/mimir/worklink-opencode/opencode.json <<'JSON'
{"model":"proof/model","provider":{"proof":{"endpoint":"https://proof.invalid","apiKey":"{env:PROOF_TOKEN}"},"unrelated":{"apiKey":"must-not-project"}}}
JSON
            cat > /home/mimir/worklink-opencode/data/opencode/auth.json <<'JSON'
{"proof":{"type":"api","key":"projected-secret"},"unrelated":{"type":"api","key":"must-not-project"}}
JSON
            chown mimir:mimir /home/mimir/worklink-opencode/opencode.json /home/mimir/worklink-opencode/data/opencode/auth.json
            chmod 0600 /home/mimir/worklink-opencode/opencode.json /home/mimir/worklink-opencode/data/opencode/auth.json
            install -d -o worklink -g worklink -m 0770 /tmp/worklink-negative-control/a /tmp/worklink-negative-control/b
            cat > /tmp/opencode-proof <<'PY'
#!/opt/mimir-worklink/venv/bin/python
import json
import os
from pathlib import Path
import subprocess
import sys

if os.geteuid() != 1002:
    raise SystemExit("coding CLI did not run as worklink")
if "--dir" not in sys.argv or sys.argv[sys.argv.index("--dir") + 1] != ".":
    raise SystemExit(f"coding CLI did not receive the FD-anchored checkout: {sys.argv}")
home = Path(os.environ["HOME"])
if home.parent != Path("/var/lib/mimir-worklink/homes"):
    raise SystemExit("coding CLI received an invalid HOME")
config = json.loads((home / ".config/opencode/opencode.json").read_text())
auth = json.loads((home / ".local/share/opencode/auth.json").read_text())
if config != {"model": "proof/model", "provider": {"proof": {"endpoint": "https://proof.invalid", "apiKey": "{env:PROOF_TOKEN}"}}}:
    raise SystemExit("selected provider configuration was not projected exactly")
if auth != {"proof": {"type": "api", "key": "projected-secret"}}:
    raise SystemExit("selected provider auth was not projected exactly")
if os.environ.get("PROOF_TOKEN") != "provider-reference":
    raise SystemExit("selected provider environment reference was not projected")
checkout = Path.cwd()
control = Path("/tmp/worklink-negative-control")
negative_control = subprocess.run(
    ["sh", "-c", "printf detector-live > ../b/cross-write"],
    cwd=control / "a",
)
if (
    negative_control.returncode != 0
    or (control / "b/cross-write").read_text() != "detector-live"
):
    raise SystemExit("sibling-access negative control did not detect a cross-write")
sibling_relative = Path("../../1411-1/checkout")
sibling_absolute = checkout.parent.parent / "1411-1" / "checkout"
checks = [
    ["cat", str(sibling_relative / "sibling-canary")],
    ["sh", "-c", f"printf attacked > {sibling_relative / 'sibling-canary'}"],
    ["rm", "-f", str(sibling_relative / "sibling-canary")],
    ["cat", str(sibling_absolute / "sibling-canary")],
    ["sh", "-c", f"printf attacked > {sibling_absolute / 'sibling-canary'}"],
    ["rm", "-f", str(sibling_absolute / "sibling-canary")],
]
for command in checks:
    if subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        raise SystemExit(f"worker reached concurrent sibling checkout: {command}")
parent = home.parent
checks = [
    ["ls", str(parent)],
    ["touch", str(parent / "parent-write")],
    ["touch", str(parent / "sibling")],
    ["mv", str(home), str(parent / "moved")],
    ["rmdir", str(home)],
    ["cat", "/home/mimir/worklink-canary"],
    ["sh", "-c", "printf attacked > /home/mimir/worklink-canary"],
]
for command in checks:
    if subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        raise SystemExit(f"worker operation unexpectedly succeeded: {command}")
checkout.joinpath("tracked").write_text("worker-modified")
checkout.joinpath("created").write_text("worker-created")
checkout.joinpath("deleted").unlink()
print(f"build-euid={os.geteuid()}")
PY
            chmod 0755 /tmp/opencode-proof
        """
        docker_exec("/bin/sh", "-ceu", setup)
        docker_exec(
            "/bin/sh", "-c", "cat > /tmp/worklink-runtime-proof.py && chmod 0644 /tmp/worklink-runtime-proof.py",
            input_text=CONTROLLER_PROOF,
        )
        docker_exec(
            "/bin/sh", "-ceu",
            "PROOF_TOKEN=provider-reference /opt/mimir-worklink/venv/bin/python /tmp/worklink-runtime-proof.py",
            user="mimir",
        )
        docker_exec("/bin/sh", "-ceu", """
            test "$(cat /home/mimir/worklink-canary)" = controller-reset
            test -z "$(find /var/lib/mimir-worklink/homes -mindepth 1 -maxdepth 1 -print -quit)"
            test -z "$(find /var/lib/mimir-worklink/checkouts -mindepth 2 -maxdepth 2 -print -quit)"
        """)
        print("worklink shipped-image identity proof passed")
    finally:
        subprocess.run(["docker", "rm", "--force", CONTAINER], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["docker", "image", "rm", "--force", IMAGE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
