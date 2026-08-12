from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = (
    "test_acp_bootstrap.py",
    "test_acp_stdio.py",
    "test_acp_packaging.py",
    "test_acp_profiles.py",
    "test_acp_credentials.py",
    "test_acp_proxy.py",
    "test_acp_ssh.py",
    "test_acp_relay.py",
    "test_acp_dependency_closure.py",
    "test_acp_shutdown.py",
)
MODULES = (
    "mimir.acp.__init__", "mimir.acp.__main__", "mimir.acp.agent", "mimir.acp.bootstrap",
    "mimir.acp.bridge", "mimir.acp.credentials", "mimir.acp.daemon", "mimir.acp.host",
    "mimir.acp.journal", "mimir.acp.profiles", "mimir.acp.proxy", "mimir.acp.relay",
    "mimir.acp.sdk", "mimir.acp.session_store", "mimir.acp.ssh", "mimir.acp.stdio",
    "mimir.acp.transport", "mimir.acp.updates",
)


def clean_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"} and not key.startswith("MIMIR_")}


def run(command: list[str], *, cwd: Path = ROOT, env: dict[str, str] | None = None, input: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    print("+", *command, flush=True)
    return subprocess.run(command, cwd=cwd, env=env, input=input, capture_output=input is not None, check=True)


def _probe(python: Path, work: Path, env: dict[str, str]) -> dict[str, object]:
    code = f"""import importlib.metadata as metadata,importlib.util as util,json
modules={list(MODULES)!r}
origins={{}}
for name in ['acp','keyring',*modules]:
 spec=util.find_spec(name)
 assert spec is not None
 origins[name]=spec.origin or next(iter(spec.submodule_search_locations))
 __import__(name)
distribution=metadata.distribution('mimir-agent')
entries={{entry.name:entry.value for entry in distribution.entry_points if entry.group=='console_scripts'}}
requires=distribution.requires or []
print(json.dumps({{'origins':origins,'entries':entries,'requires':requires}}))
"""
    completed = subprocess.run([str(python), "-c", code], cwd=work, env=env, text=True, capture_output=True, check=True)
    return json.loads(completed.stdout)


def _assert_metadata(probe: dict[str, object], temporary: Path) -> Path:
    assert probe["entries"] == {"mimir": "mimir.entrypoint:main", "mimir-agent": "mimir.entrypoint:main"}
    requirements = probe["requires"]
    assert sum(item == "agent-client-protocol==0.12.0" for item in requirements) == 1
    assert sum(item == "keyring==25.7.0" for item in requirements) == 1
    assert not any("extra == 'acp'" in item or 'extra == "acp"' in item for item in requirements)
    origins = {name: Path(origin).resolve() for name, origin in probe["origins"].items()}
    for name, origin in origins.items():
        if temporary.resolve() not in origin.parents:
            raise AssertionError(f"{name} resolved outside clean environment: {origin}")
        if ROOT.resolve() in origin.parents:
            raise AssertionError(f"{name} leaked from checkout: {origin}")
    return origins["mimir.acp.__init__"].parents[1]


def _install_native_fixture(python: Path, work: Path, env: dict[str, str]) -> None:
    site = subprocess.run([str(python), "-c", "import site; print(site.getsitepackages()[0])"], cwd=work, env=env, text=True, capture_output=True, check=True).stdout.strip()
    Path(site, "sitecustomize.py").write_text(
        "import keyring\n"
        "class Keyring:\n"
        "    __module__='keyring.backends.SecretService'\n"
        "    priority=1\n"
        "    def get_password(self,service,user): return 'installed-secret'\n"
        "    def set_password(self,service,user,value): pass\n"
        "    def delete_password(self,service,user): pass\n"
        "keyring.get_keyring=lambda:Keyring()\n"
    )


def _local_round_trip(command: list[str], python: Path, work: Path, env: dict[str, str], config: Path) -> None:
    home = work / "home"
    socket = home / ".mimir" / "acp" / "daemon.sock"
    socket.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(socket.parent, 0o700)
    server = """import asyncio,json,os,pathlib,sys
path=pathlib.Path(sys.argv[1])
async def main():
 done=asyncio.Event()
 async def handle(reader,writer):
  message=json.loads(await reader.readline())
  assert message['params']['_meta']=={'mimir.webKey':'installed-secret','other':1}
  writer.write(b'{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"ok\":true}}\\n')
  await writer.drain(); writer.close(); await writer.wait_closed(); done.set()
 service=await asyncio.start_unix_server(handle,path=str(path))
 await done.wait(); service.close(); await service.wait_closed()
asyncio.run(main())
"""
    server_process = subprocess.Popen([str(python), "-c", server, str(socket)], cwd=work, env=env)
    try:
        for _ in range(200):
            if socket.exists():
                break
            import time
            time.sleep(0.01)
        payload = b'{"jsonrpc":"2.0","id":1,"method":"authenticate","params":{"methodId":"mimir-web-key","_meta":{"mimir.fake":"forged","other":1}}}\n'
        completed = subprocess.run(command, cwd=work, env=env, input=payload, capture_output=True, check=True, timeout=15)
        assert completed.stdout == b'{"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n'
        assert b"installed-secret" not in completed.stdout + completed.stderr
    finally:
        server_process.wait(timeout=15)


def _relay_round_trip(command: list[str], python: Path, work: Path, env: dict[str, str]) -> None:
    home = work / "relay-home"
    socket = home / ".mimir" / "acp" / "daemon.sock"
    socket.parent.mkdir(parents=True, mode=0o700)
    os.chmod(socket.parent, 0o700)
    server = """import asyncio,pathlib,sys
async def main():
 async def handle(reader,writer):
  writer.write(await reader.read()); await writer.drain(); writer.close(); await writer.wait_closed()
 service=await asyncio.start_unix_server(handle,path=sys.argv[1])
 async with service: await service.serve_forever()
asyncio.run(main())
"""
    process = subprocess.Popen([str(python), "-c", server, str(socket)], cwd=work, env=env)
    try:
        for _ in range(200):
            if socket.exists(): break
            import time
            time.sleep(0.01)
        completed = subprocess.run([*command, "relay", "--home", str(home)], cwd=work, env=env, input=b"relay-bytes", capture_output=True, check=True, timeout=15)
        assert completed.stdout == b"relay-bytes" and completed.stderr == b""
    finally:
        process.terminate()
        process.wait(timeout=5)


def smoke(artifact: Path) -> None:
    artifact = artifact.resolve()
    with tempfile.TemporaryDirectory(prefix="mimir-acp-install-") as value:
        temporary = Path(value)
        venv = temporary / "venv"
        work = temporary / "work"
        work.mkdir()
        env = clean_env()
        run(["uv", "venv", "--python", sys.executable, str(venv)], cwd=work, env=env)
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        run(["uv", "pip", "install", "--python", str(python), f"{artifact}[acp]"], cwd=work, env=env)
        probe = _probe(python, work, env)
        package = _assert_metadata(probe, temporary)
        closure_spec = importlib.util.spec_from_file_location("closure", ROOT / ".github" / "acp_dependency_closure.py")
        assert closure_spec and closure_spec.loader
        closure = importlib.util.module_from_spec(closure_spec)
        closure_spec.loader.exec_module(closure)
        closure.assert_policy(closure.module_paths(package))
        assert not Path(package, "acp", "composition.py").exists()

        short = Path(tempfile.gettempdir()) / f"ma-{uuid.uuid4().hex[:8]}"
        short.mkdir(mode=0o700)
        config = short / "config"
        env["XDG_CONFIG_HOME"] = str(config)
        bindir = python.parent
        launches = [
            [str(bindir / "mimir"), "acp", "--help"],
            [str(bindir / "mimir"), "acp", "profile", "list"],
            [str(bindir / "mimir-agent"), "acp", "--help"],
            [str(bindir / "mimir-agent"), "acp", "profile", "list"],
            [str(bindir / "mimir-agent"), "acp", "relay", "--help"],
            [str(python), "-m", "mimir.acp", "--help"],
            [str(python), "-m", "mimir.acp", "profile", "list"],
        ]
        for command in launches:
            run(command, cwd=work, env=env)
        completed = subprocess.run([str(python), "-m", "mimir.acp", "acp"], cwd=work, env=env, capture_output=True)
        assert completed.returncode == 2 and completed.stdout == b"" and b"usage:" in completed.stderr

        try:
            work = short / "work"
            work.mkdir()
            run([str(bindir / "mimir"), "acp", "profile", "set", "default", "--home", str(work / "home")], cwd=work, env=env)
            _install_native_fixture(python, work, env)
            _local_round_trip([str(bindir / "mimir"), "acp"], python, work, env, config)
            _local_round_trip([str(python), "-m", "mimir.acp"], python, work, env, config)
            _relay_round_trip([str(bindir / "mimir-agent"), "acp"], python, work, env)
        finally:
            import shutil
            shutil.rmtree(short, ignore_errors=True)
        work = temporary / "work"

        ssh_probe = """from pathlib import Path
from mimir.acp.profiles import Profile,RemoteProfile
from mimir.acp.ssh import build_ssh_argv
import tempfile
with tempfile.TemporaryDirectory() as value:
 root=Path(value); ssh=root/'ssh'; identity=root/'id'; known=root/'known'
 for path,mode in ((ssh,0o755),(identity,0o600),(known,0o600)): path.write_text(''); path.chmod(mode)
 argv=build_ssh_argv(Profile('remote',Path('/remote'),RemoteProfile('example.com','agent',22,identity,known)),ssh)
 assert argv[-1]=='mimir-agent acp relay --home /remote'
"""
        run([str(python), "-c", ssh_probe], cwd=work, env=env)


def slice_verify() -> None:
    for test in TESTS:
        run(["uv", "run", "pytest", "-q", f"tests/{test}", "--tb=short"])
    run(["uv", "run", "pytest", "-q", "--tb=short"])
    run(["npm", "ci"])
    run(["npm", "run", "build"])
    with tempfile.TemporaryDirectory(prefix="mimir-acp-build-") as value:
        output = Path(value)
        direct = output / "direct"
        direct.mkdir()
        run(["uv", "build", "--out-dir", str(output)])
        run(["uv", "build", "--wheel", "--out-dir", str(direct)])
        sdist_wheel = next(path for path in output.glob("*.whl"))
        wheel = next(direct.glob("*.whl"))
        sdist = next(output.glob("*.tar.gz"))
        run([sys.executable, ".github/assert_wheel_contents.py", "--sdist-wheel", str(sdist_wheel), "--direct-wheel", str(wheel)])
        smoke(wheel)
        smoke(sdist)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("artifact", nargs="?", type=Path)
    parser.add_argument("--slice-verify", action="store_true")
    args = parser.parse_args()
    if args.slice_verify:
        slice_verify()
    elif args.artifact:
        smoke(args.artifact)
    else:
        parser.error("artifact or --slice-verify is required")


if __name__ == "__main__":
    main()
