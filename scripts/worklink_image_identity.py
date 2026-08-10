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
import os
from pathlib import Path
import uuid
from mimir.worklink.worker_client import WorkerClient

CHECKOUT = Path("/var/lib/mimir-worklink/checkouts/" + "a" * 64 + "/1410-1")

class Capability:
    path = CHECKOUT
    issue_id = 1410
    attempt = 1
    def __init__(self):
        self.fd = os.open(CHECKOUT, os.O_RDONLY | os.O_DIRECTORY)
        observed = os.fstat(self.fd)
        self.device = observed.st_dev
        self.inode = observed.st_ino
    def verify(self, local_checkout):
        if Path(local_checkout) != CHECKOUT:
            raise RuntimeError("checkout mismatch")
    def duplicate_fd(self):
        return os.dup(self.fd)

async def execute(label, body):
    capability = Capability()
    try:
        process = await WorkerClient(capability).launch(
            local_checkout=CHECKOUT,
            argv=("/bin/sh", "-ceu", body),
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "USER": "worklink", "LOGNAME": "worklink", "SHELL": "/bin/sh", "LANG": "C.UTF-8"},
            identifier=str(uuid.uuid4()),
        )
        stdout, stderr, returncode = await asyncio.gather(
            process.stdout.read(), process.stderr.read(), process.wait()
        )
        if returncode != 0:
            raise RuntimeError(f"{label} failed ({returncode}) stdout={stdout!r} stderr={stderr!r}")
        output = stdout.decode()
        if f"{label}-euid=1002" not in output:
            raise RuntimeError(f"{label} did not report worker identity: {output!r}")
    finally:
        os.close(capability.fd)

async def main():
    if os.geteuid() != 1001:
        raise RuntimeError("controller proof is not running as mimir")
    await execute("build", r'''test "$(id -u)" = 1002
printf 'build-euid=%s\n' "$(id -u)"
case "$HOME" in /var/lib/mimir-worklink/homes/*) ;; *) exit 20 ;; esac
cd "$HOME"
! ls "$(dirname "$HOME")"
! touch "$(dirname "$HOME")/parent-write"
! touch "$(dirname "$HOME")/sibling"
! mv "$HOME" "$(dirname "$HOME")/moved"
! rmdir "$HOME"
! cat /home/mimir/worklink-canary
! printf attacked > /home/mimir/worklink-canary
printf worker-modified > "$OLDPWD/tracked"
printf worker-created > "$OLDPWD/created"
rm "$OLDPWD/deleted"
''')
    if Path("/home/mimir/worklink-canary").read_text() != "controller-reset":
        raise RuntimeError("worker changed controller canary")
    if CHECKOUT.joinpath("tracked").read_text() != "worker-modified":
        raise RuntimeError("controller could not continue from worker edit")
    CHECKOUT.joinpath("controller-continuation").write_text("continued")
    await execute("gate", r'''test "$(id -u)" = 1002
printf 'gate-euid=%s\n' "$(id -u)"
test "$(cat tracked)" = worker-modified
test "$(cat created)" = worker-created
test "$(cat controller-continuation)" = continued
! test -e deleted
! cat /home/mimir/worklink-canary
''')

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
        process_table = run(["docker", "top", CONTAINER, "-eo", "pid,user,args"]).stdout
        if not any(line.split(maxsplit=2)[1] in {"1001", "mimir"} and "mimir run" in line for line in process_table.splitlines()[1:]):
            raise RuntimeError(f"live mimir service is not uid 1001\n{process_table}")
        if not any(line.split(maxsplit=2)[1] == "root" and "mimir.worklink.worker_exec" in line for line in process_table.splitlines()[1:]):
            raise RuntimeError(f"live worker executor is not root\n{process_table}")

        setup = """
            set -eu
            /command/s6-setuidgid mimir sh -ceu 'printf controller-writable > /home/mimir/worklink-canary; printf controller-reset > /home/mimir/worklink-canary'
            test "$(cat /home/mimir/worklink-canary)" = controller-reset
            /command/s6-setuidgid mimir sh -ceu '
              rm -rf /home/mimir/worklink-source
              git init -q /home/mimir/worklink-source
              cd /home/mimir/worklink-source
              printf base > tracked
              printf remove > deleted
              git add tracked deleted
              git -c user.name=test -c user.email=test@example.com commit -qm base
            '
            parent=/var/lib/mimir-worklink/checkouts/""" + "a" * 64 + """
            checkout="$parent/1410-1"
            rm -rf "$parent"
            install -d -o mimir -g worklink -m 0710 "$parent"
            /command/s6-setuidgid mimir git clone --local --no-hardlinks -q /home/mimir/worklink-source "$checkout"
            source_object=$(find /home/mimir/worklink-source/.git/objects -type f | head -n 1)
            relative=${source_object#/home/mimir/worklink-source/.git/objects/}
            test "$(stat -c %d:%i "$checkout/.git/objects/$relative")" != "$(stat -c %d:%i "$source_object")"
            chown -R mimir:worklink "$checkout"
            find "$checkout" -type d -exec chmod 2770 {} +
            find "$checkout" -type f -exec chmod 0660 {} +
        """
        docker_exec("/bin/sh", "-ceu", setup)
        docker_exec(
            "/bin/sh", "-c", "cat > /tmp/worklink-runtime-proof.py && chmod 0644 /tmp/worklink-runtime-proof.py",
            input_text=CONTROLLER_PROOF,
        )
        docker_exec(
            "/bin/sh", "-ceu",
            "PYTHONPATH=/opt/mimir-worklink/source /opt/mimir-worklink/venv/bin/python /tmp/worklink-runtime-proof.py",
            user="1001:1001",
        )
        checkout = "/var/lib/mimir-worklink/checkouts/" + "a" * 64 + "/1410-1"
        docker_exec("/bin/sh", "-ceu", f"""
            test "$(cat /home/mimir/worklink-canary)" = controller-reset
            test "$(cat {checkout}/tracked)" = worker-modified
            test "$(cat {checkout}/controller-continuation)" = continued
            test -z "$(find /var/lib/mimir-worklink/homes -mindepth 1 -maxdepth 1 -print -quit)"
        """)
        print("worklink shipped-image identity proof passed")
    finally:
        subprocess.run(["docker", "rm", "--force", CONTAINER], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["docker", "image", "rm", "--force", IMAGE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
