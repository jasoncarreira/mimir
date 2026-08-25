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
import subprocess

from mimir.worklink.backends.base import WorkOrder
from mimir.worklink.backends.opencode import OpenCodeBackend
from mimir.worklink.checkout import cleanup_checkout, create_isolated_checkout
from mimir.worklink.compute import LocalSubprocessComputeBackend
from mimir.worklink.evidence import observe_evidence
from mimir.worklink.safe_git import ControllerGitPublication

REPO = Path("/workspace/mimir")
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
    if lease.worker_authorized or lease.authorization is not None:
        raise RuntimeError("Worklink unexpectedly selected the contained checkout path")
    if sibling.worker_authorized or sibling.authorization is not None:
        raise RuntimeError("concurrent checkout unexpectedly selected the contained path")
    sibling.path.joinpath("sibling-canary").write_text("sibling-original")
    source_object = next(REPO.joinpath(".git/objects").glob("[0-9a-f][0-9a-f]/*"))
    checkout_object = lease.path / ".git/objects" / source_object.relative_to(REPO / ".git/objects")
    if source_object.stat().st_ino == checkout_object.stat().st_ino:
        raise RuntimeError("issued checkout retained a source hardlink")
    checkout_fd = os.open(lease.path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        publication = ControllerGitPublication.capture(
            checkout_fd, REPO, lease.branch, METADATA
        )
    finally:
        os.close(checkout_fd)
    compute = LocalSubprocessComputeBackend.for_path_checkout(1002)
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
    publication.run("add", "-A", check=True)
    publication.run("commit", "-m", "runtime proof", check=True)
    publication.push(check=True)
    if Path("/tmp/worklink-publication-attack").exists():
        raise RuntimeError("controller publication executed worker-planted Git metadata")
    trusted_head = publication.run("rev-parse", "HEAD", check=True).stdout.strip()
    remote_head = os.popen(
        "git --git-dir=/home/mimir/worklink-remote.git rev-parse refs/heads/issue/1410-a1"
    ).read().strip()
    if remote_head != trusted_head:
        raise RuntimeError("controller publication followed the worker push URL")
    if os.system(
        "git --git-dir=/tmp/worklink-hostile.git show-ref --verify refs/heads/issue/1410-a1 >/dev/null 2>&1"
    ) == 0:
        raise RuntimeError("worker-controlled remote received the publication")
    if Path("/home/mimir/worklink-canary").read_text() != "controller-reset":
        raise RuntimeError("worker changed controller canary")
    if sibling.path.joinpath("sibling-canary").read_text() != "sibling-original":
        raise RuntimeError("worker changed concurrent sibling checkout")
    cleanup_checkout(lease, outcome="completed")
    cleanup_checkout(sibling, outcome="completed")
    publication.close()
    if lease.path.exists():
        raise RuntimeError("production checkout cleanup failed")

asyncio.run(main())
"""

SPAWN_PROOF = r"""
from __future__ import annotations
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import uuid

from mimir.tools.registry import set_spawn_config, spawn_open_code

SEED = Path("/workspace/mimir")
ARTIFACTS = Path("/home/mimir/worklink-spawn-artifacts")
CONFIG = Path("/home/mimir/worklink-opencode/opencode.json")
AUTH = Path("/home/mimir/worklink-opencode/data/opencode/auth.json")
CANARY = Path("/home/mimir/worklink-canary")
OAUTH = (b"oauth-access-proof", b"oauth-refresh-proof")

async def main():
    if os.geteuid() != 1001:
        raise RuntimeError("spawn controller proof is not running as mimir")
    os.environ.update({
        "OPENCODE_CONFIG": str(CONFIG),
        "XDG_DATA_HOME": str(AUTH.parents[1]),
        "MIMIR_MODEL_SPEC": "proof:model",
    })
    set_spawn_config({
        "default_cwd": SEED,
        "artifact_root": ARTIFACTS,
        "opencode_config_path": CONFIG,
    })
    source_head = (SEED / ".git/HEAD").read_bytes()

    def seed_status() -> bytes:
        return subprocess.run(
            ["git", "-C", str(SEED), "status", "--porcelain=v1", "-z"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        ).stdout

    source_status = seed_status()
    raw = await spawn_open_code.ainvoke({
        "prompt": "generate and execute the containment proof payload",
        "cwd": str(SEED),
        "timeout_s": 30,
        "name": "shipped-image-proof",
        "model": "proof/model",
        "artifact_root": str(ARTIFACTS),
    })
    result = json.loads(raw)
    if result["status"] != "succeeded" or result["exit_code"] != 0:
        raise RuntimeError(f"live spawn failed: {result}")
    if not result["run_id"].startswith("opencode-"):
        raise RuntimeError("live spawn did not preserve its public run handle")
    if result["artifact_dir"] != result["run_id"]:
        raise RuntimeError("live spawn did not return its relative artifact handle")
    if "spawn-euid=1002" not in result["stdout"]:
        raise RuntimeError("live fake OpenCode did not report worker identity")
    identifier_line = next(
        (line for line in result["stdout"].splitlines() if line.startswith("spawn-identifier=")),
        None,
    )
    if identifier_line is None:
        raise RuntimeError("live fake OpenCode did not report its execution identifier")
    identifier = uuid.UUID(identifier_line.partition("=")[2])
    if identifier.version != 4 or str(identifier) != identifier_line.partition("=")[2]:
        raise RuntimeError("live fake OpenCode did not receive a canonical UUIDv4 identifier")
    proposal = result.get("proposal")
    if not isinstance(proposal, dict):
        raise RuntimeError("live spawn did not produce a proposal")
    patch = base64.b64decode(proposal["patch"], validate=True)
    if proposal.get("schema_version") != 1 or proposal.get("kind") != "git_binary_patch":
        raise RuntimeError("live spawn proposal schema is invalid")
    if proposal.get("byte_length") != len(patch):
        raise RuntimeError("live spawn proposal length is invalid")
    if proposal.get("sha256") != hashlib.sha256(patch).hexdigest():
        raise RuntimeError("live spawn proposal digest is invalid")
    for marker in (b"spawn-payload-executed", b"spawn-worker-uid"):
        if marker not in patch:
            raise RuntimeError(f"live spawn proposal omitted {marker!r}")
    artifact = ARTIFACTS / result["artifact_dir"]
    observed = raw.encode()
    for path in artifact.rglob("*"):
        if path.is_file():
            observed += path.read_bytes()
    forbidden = (*OAUTH, os.fsencode(CONFIG), os.fsencode(AUTH))
    for material in forbidden:
        if material in observed:
            raise RuntimeError("spawn output or artifacts disclosed OAuth material or controller paths")
    if CANARY.read_text() != "controller-reset":
        raise RuntimeError("generated OpenCode payload changed controller canary")
    if (SEED / ".git/HEAD").read_bytes() != source_head:
        raise RuntimeError("spawn changed the seed repository HEAD")
    if seed_status() != source_status:
        raise RuntimeError("spawn changed the seed repository working tree")
    for path in sorted(ARTIFACTS.rglob("*"), reverse=True):
        if path.is_file() or path.is_symlink():
            path.unlink()
        else:
            path.rmdir()
    ARTIFACTS.rmdir()

asyncio.run(main())
"""


def source_ref() -> str:
    """Return the exact remote ref expected to contain this checkout's HEAD."""
    explicit = os.environ.get("MIMIR_GIT_REF") or os.environ.get("GITHUB_REF")
    if explicit:
        return explicit
    try:
        branch = run(["git", "symbolic-ref", "--quiet", "--short", "HEAD"]).stdout.strip()
        tracked_ref = run(["git", "config", "--get", f"branch.{branch}.merge"]).stdout.strip()
    except RuntimeError as exc:
        raise RuntimeError(
            "set MIMIR_GIT_REF to the fully qualified remote branch, tag, or pull ref "
            "that contains this commit"
        ) from exc
    if not tracked_ref.startswith("refs/"):
        raise RuntimeError(f"tracked Git ref is not fully qualified: {tracked_ref!r}")
    return tracked_ref


def main() -> None:
    run(["docker", "info"], timeout=30)
    source_commit = run(["git", "rev-parse", "HEAD"]).stdout.strip()
    git_ref = source_ref()
    if run(["git", "status", "--porcelain=v1"]).stdout.strip():
        raise RuntimeError("live image proof requires a clean source checkout")
    service = (ROOT / "deploy/s6-overlay/s6-rc.d/mimir/run").read_text()
    expected = 'exec s6-setuidgid mimir mimir run --home "${MIMIR_HOME:-/home/mimir/agent}"'
    if expected not in service:
        raise RuntimeError("controller s6 service no longer drops to mimir")
    try:
        run([
            "docker", "build",
            "--build-arg", f"MIMIR_GIT_REF={git_ref}",
            "--build-arg", f"MIMIR_CONTROLLER_COMMIT={source_commit}",
            "--build-arg", f"MIMIR_EXECUTOR_COMMIT={source_commit}",
            "--tag", IMAGE, ".",
        ], timeout=1200)
        run(["docker", "run", "--detach", "--env", "HOME=/home/mimir", "--name", CONTAINER, IMAGE])
        wait_for_runtime()
        docker_exec("/bin/sh", "-ceu", """
            test "$(id -u mimir)" = 1001
            test "$(id -u worklink)" = 1002
            test "$(stat -c %a:%u:%g /home/mimir)" = 700:1001:1001
            test "$(stat -c %U:%G /usr/local/libexec/worklink-execd)" = root:root
            test "$(stat -c %U:%G /opt/mimir-worklink/venv/bin/python)" = root:root
            test "$(stat -c %U:%G /opt/mimir-worklink/uv-cache)" = root:root
            test "$(stat -c %a /opt/mimir-worklink/uv-cache)" = 755
            test "$(cat /opt/mimir-worklink/executor-source-commit)" = "SOURCE_COMMIT"
            test "$(stat -c %a:%u:%g /var/lib/mimir-worklink/homes)" = 710:0:1002
        """.replace("SOURCE_COMMIT", source_commit))
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

        setup = r"""
            set -eu
            install -d -o mimir -g mimir -m 0755 /workspace
            /command/s6-setuidgid mimir sh -ceu 'printf controller-writable > /home/mimir/worklink-canary; printf controller-reset > /home/mimir/worklink-canary'
            test "$(cat /home/mimir/worklink-canary)" = controller-reset
            /command/s6-setuidgid mimir sh -ceu '
              rm -rf /workspace/mimir /workspace/.worklink /home/mimir/worklink-remote.git /home/mimir/worklink-opencode /home/mimir/worklink-publication
              mkdir -p /workspace
              git init --bare -q /home/mimir/worklink-remote.git
              git init -q /workspace/mimir
              cd /workspace/mimir
              git remote add origin /home/mimir/worklink-remote.git
              printf base > tracked
              printf remove > deleted
              git add tracked deleted
              git -c user.name=test -c user.email=test@example.com commit -qm base
              git branch -M main
              git push -q -u origin main
              git config user.name test
              git config user.email test@example.com
              git config credential.helper "!f() { echo username=controller; echo password=trusted; }; f"
              mkdir -p /home/mimir/worklink-opencode/data/opencode
              rm -rf /tmp/worklink-hostile.git /tmp/worklink-publication-attack
              git init --bare -q /tmp/worklink-hostile.git
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
if config != {"model": "proof/model", "provider": {"proof": {"endpoint": "https://proof.invalid", "apiKey": "provider-reference"}}}:
    raise SystemExit("selected provider configuration was not projected exactly")
if auth != {"proof": {"type": "api", "key": "projected-secret"}}:
    raise SystemExit("selected provider auth was not projected exactly")
if "PROOF_TOKEN" in os.environ:
    raise SystemExit("selected provider credential leaked into the worker environment")
checkout = Path.cwd()
checkout_fd = os.open(".", os.O_RDONLY | os.O_DIRECTORY)
os.chdir(home)
os.fchdir(checkout_fd)
control = Path("/tmp/worklink-negative-control")
os.chdir(control / "a")
(Path("../b") / "cross-write").write_text("detector-live")
if (Path("../b") / "cross-write").read_text() != "detector-live":
    raise SystemExit("sibling-access negative control did not detect a cross-write")
os.fchdir(checkout_fd)
os.close(checkout_fd)
sibling = checkout.parent / "1411-1" / "sibling-canary"
if sibling.read_text() != "sibling-original":
    raise SystemExit("worker could not reach the intentionally shared sibling checkout")
parent = home.parent
checks = [
    ["ls", str(parent)],
    ["touch", str(parent / "parent-write")],
    ["touch", str(parent / "sibling")],
    ["mv", str(home), str(parent / "moved")],
    ["rmdir", str(home)],
    ["cat", "/home/mimir/worklink-canary"],
    ["sh", "-c", "printf attacked > /home/mimir/worklink-canary"],
    ["sh", "-c", "printf attacked > /workspace/mimir/tracked"],
]
for command in checks:
    if subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        raise SystemExit(f"worker operation unexpectedly succeeded: {command}")
Path("tracked").write_text("worker-modified")
Path("created").write_text("worker-created")
Path("deleted").unlink()
hooks = Path("worker-hooks")
hooks.mkdir()
for name in ("pre-commit", "pre-push"):
    hook = hooks / name
    hook.write_text("#!/bin/sh\ntouch /tmp/worklink-publication-attack\nexit 91\n")
    hook.chmod(0o755)
# The worker runs as ``worklink`` while the checkout is owned by ``mimir``, so bare
# git refuses with "detected dubious ownership" and plants nothing. Name the checkout
# explicitly and mark it safe: a hostile worker would do exactly this, and a proof that
# cannot plant proves nothing.
checkout = str(Path.cwd().resolve())
git = ["git", "-c", f"safe.directory={checkout}", "-C", checkout]
hostile_helper = "!f() { touch /tmp/worklink-publication-attack; echo username=worker; echo password=hostile; }; f"
subprocess.run([*git, "config", "--local", "core.hooksPath", str(hooks.resolve())], check=True)
subprocess.run([*git, "config", "--local", "credential.helper", hostile_helper], check=True)
subprocess.run([*git, "remote", "set-url", "--push", "origin", "/tmp/worklink-hostile.git"], check=True)

# Read every planted value back. Without this the proof passes vacuously whenever the
# plant silently fails, which is the failure mode it exists to rule out.
def planted(args, expected):
    got = subprocess.run([*git, *args], capture_output=True, text=True, check=True).stdout.strip()
    if expected not in got:
        raise SystemExit(f"hostile setup did not persist: {args} -> {got!r}")

planted(["config", "--local", "--get", "core.hooksPath"], str(hooks.resolve()))
planted(["config", "--local", "--get", "credential.helper"], "worklink-publication-attack")
planted(["remote", "get-url", "--push", "origin"], "/tmp/worklink-hostile.git")
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
        spawn_setup = r"""
            set -eu
            cat > /home/mimir/worklink-opencode/opencode.json <<'JSON'
{"model":"proof/model","provider":{"proof":{"endpoint":"https://proof.invalid"}}}
JSON
            cat > /home/mimir/worklink-opencode/data/opencode/auth.json <<'JSON'
{"proof":{"type":"oauth","access":"oauth-access-proof","refresh":"oauth-refresh-proof","expires":4102444800000}}
JSON
            chown mimir:mimir /home/mimir/worklink-opencode/opencode.json /home/mimir/worklink-opencode/data/opencode/auth.json
            chmod 0600 /home/mimir/worklink-opencode/opencode.json /home/mimir/worklink-opencode/data/opencode/auth.json
            cat > /usr/local/bin/opencode <<'PY'
#!/opt/mimir-worklink/venv/bin/python
import json
import os
from pathlib import Path
import sys
import uuid

if os.geteuid() != 1002:
    raise SystemExit("spawn fake did not run as worklink")
if "--dir" not in sys.argv or sys.argv[sys.argv.index("--dir") + 1] != ".":
    raise SystemExit(f"spawn fake did not receive the FD-anchored checkout: {sys.argv}")
home = Path(os.environ["HOME"])
identifier = uuid.UUID(home.name)
if identifier.version != 4 or str(identifier) != home.name:
    raise SystemExit("spawn fake did not receive a canonical UUIDv4 identifier")
config = json.loads((home / ".config/opencode/opencode.json").read_text())
auth = json.loads((home / ".local/share/opencode/auth.json").read_text())
expected_config = {"model": "proof/model", "provider": {"proof": {"endpoint": "https://proof.invalid"}}}
expected_auth = {"proof": {"type": "oauth", "access": "oauth-access-proof", "refresh": "oauth-refresh-proof", "expires": 4102444800000}}
if config != expected_config:
    raise SystemExit("spawn fake did not read the selected provider config projection")
if auth != expected_auth:
    raise SystemExit("spawn fake did not read the OAuth projection")
payload = Path("generated-containment-proof.py")
payload.write_text(
    "import os\n"
    "import pwd\n"
    "from pathlib import Path\n"
    "controller_home = Path(pwd.getpwnam('mimir').pw_dir)\n"
    "try:\n"
    "    (controller_home / 'worklink-canary').write_text('attacked')\n"
    "except OSError:\n"
    "    pass\n"
    "Path('spawn-payload-executed').write_text('executed')\n"
    "Path('spawn-worker-uid').write_text(str(os.geteuid()))\n"
)
exec(compile(payload.read_bytes(), str(payload), "exec"), {"__name__": "__main__"})
print("spawn-euid=1002")
print(f"spawn-identifier={identifier}")
print("oauth-access-proof oauth-refresh-proof")
print("/home/mimir/worklink-opencode/opencode.json")
print("/home/mimir/worklink-opencode/data/opencode/auth.json", file=sys.stderr)
PY
            chmod 0755 /usr/local/bin/opencode
        """
        docker_exec("/bin/sh", "-ceu", spawn_setup)
        docker_exec(
            "/bin/sh", "-c", "cat > /tmp/worklink-spawn-proof.py && chmod 0644 /tmp/worklink-spawn-proof.py",
            input_text=SPAWN_PROOF,
        )
        docker_exec(
            "/opt/mimir-worklink/venv/bin/python", "/tmp/worklink-spawn-proof.py",
            user="mimir",
        )
        docker_exec("/bin/sh", "-ceu", """
            test "$(cat /home/mimir/worklink-canary)" = controller-reset
            test -z "$(find /var/lib/mimir-worklink/homes -mindepth 1 -maxdepth 1 -print -quit)"
            test -z "$(find /var/lib/mimir-worklink/checkouts -mindepth 2 -maxdepth 2 -print -quit)"
            test -z "$(find /var/lib/mimir-worklink/opencode-checkouts -mindepth 2 -maxdepth 2 -print -quit)"
        """)
        print("worklink shipped-image identity proof passed")
    finally:
        subprocess.run(["docker", "rm", "--force", CONTAINER], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["docker", "image", "rm", "--force", IMAGE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
