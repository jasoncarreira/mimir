from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
TESTS=("test_acp_bootstrap.py","test_acp_stdio.py","test_acp_packaging.py","test_acp_profiles.py","test_acp_credentials.py","test_acp_proxy.py","test_acp_ssh.py","test_acp_relay.py","test_acp_dependency_closure.py","test_acp_shutdown.py")

def clean_env() -> dict[str,str]:
 return {k:v for k,v in os.environ.items() if k not in {"PYTHONPATH","PYTHONHOME"} and not k.startswith("MIMIR_")}

def run(command: list[str], *, cwd: Path=ROOT, env: dict[str,str]|None=None) -> None:
 print("+",*command,flush=True); subprocess.run(command,cwd=cwd,env=env,check=True)

def smoke(artifact: Path) -> None:
 artifact=artifact.resolve()
 with tempfile.TemporaryDirectory(prefix="mimir-acp-install-") as value:
  temporary=Path(value); venv=temporary/"venv"; work=temporary/"work"; work.mkdir()
  run(["uv","venv","--python",sys.executable,str(venv)],cwd=work,env=clean_env())
  python=venv/("Scripts/python.exe" if os.name=="nt" else "bin/python")
  run(["uv","pip","install","--python",str(python),f"{artifact}[acp]"],cwd=work,env=clean_env())
  probe="""import importlib.metadata as m,importlib.util as u,json,pathlib
mods=['acp','keyring','mimir.acp.__main__','mimir.acp.agent','mimir.acp.bootstrap','mimir.acp.bridge','mimir.acp.credentials','mimir.acp.daemon','mimir.acp.host','mimir.acp.journal','mimir.acp.profiles','mimir.acp.proxy','mimir.acp.relay','mimir.acp.sdk','mimir.acp.session_store','mimir.acp.ssh','mimir.acp.stdio','mimir.acp.transport','mimir.acp.updates']
for x in mods: __import__(x)
eps={e.name:e.value for e in m.entry_points(group='console_scripts') if e.name in {'mimir','mimir-agent'}}
assert eps=={'mimir':'mimir.entrypoint:main','mimir-agent':'mimir.entrypoint:main'}
assert u.find_spec('mimir.acp.composition') is None
print(pathlib.Path(u.find_spec('mimir').submodule_search_locations[0]))"""
  result=subprocess.run([str(python),"-c",probe],cwd=work,env=clean_env(),text=True,capture_output=True,check=True)
  package=Path(result.stdout.strip())
  import importlib.util
  spec=importlib.util.spec_from_file_location("closure",ROOT/".github"/"acp_dependency_closure.py"); assert spec and spec.loader
  closure=importlib.util.module_from_spec(spec); spec.loader.exec_module(closure); closure.assert_policy(closure.module_paths(package))
  config=temporary/"config"; env=clean_env(); env["XDG_CONFIG_HOME"]=str(config)
  bindir=python.parent
  commands=[[str(bindir/"mimir"),"acp","--help"],[str(bindir/"mimir"),"acp","profile","list"],
   [str(bindir/"mimir-agent"),"acp","--help"],[str(bindir/"mimir-agent"),"acp","relay","--help"],
   [str(python),"-m","mimir.acp","--help"],[str(python),"-m","mimir.acp","profile","list"]]
  for command in commands: run(command,cwd=work,env=env)
  completed=subprocess.run([str(python),"-m","mimir.acp","acp"],cwd=work,env=env)
  if completed.returncode!=2: raise SystemExit("extra acp argument did not exit 2")

def slice_verify() -> None:
 for test in TESTS: run(["uv","run","pytest","-q",f"tests/{test}","--tb=short"])
 run(["uv","run","pytest","-q","--tb=short"])
 run(["npm","ci"]); run(["npm","run","build"])
 with tempfile.TemporaryDirectory(prefix="mimir-acp-build-") as value:
  output=Path(value); direct=output/"direct"; direct.mkdir()
  run(["uv","build","--out-dir",str(output)])
  run(["uv","build","--wheel","--out-dir",str(direct)])
  sdist_wheel=next(p for p in output.glob("*.whl")); wheel=next(direct.glob("*.whl")); sdist=next(output.glob("*.tar.gz"))
  run([sys.executable,".github/assert_wheel_contents.py","--sdist-wheel",str(sdist_wheel),"--direct-wheel",str(wheel)])
  smoke(wheel); smoke(sdist)

def main() -> None:
 parser=argparse.ArgumentParser(); parser.add_argument("artifact",nargs="?",type=Path); parser.add_argument("--slice-verify",action="store_true"); args=parser.parse_args()
 if args.slice_verify: slice_verify()
 elif args.artifact: smoke(args.artifact)
 else: parser.error("artifact or --slice-verify is required")
if __name__=="__main__": main()
