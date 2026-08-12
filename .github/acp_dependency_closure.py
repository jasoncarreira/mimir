from __future__ import annotations

import ast
from pathlib import Path
from typing import Mapping

POLICY = {
 "mimir.acp.proxy": {"mimir.acp.profiles", "mimir.acp.credentials", "mimir.acp.transport"},
 "mimir.acp.ssh": {"mimir.acp.proxy", "mimir.acp.profiles", "mimir.acp.credentials", "mimir.acp.transport"},
 "mimir.acp.relay": {"mimir.acp.transport"},
 "mimir.acp.__main__": {"mimir.acp.bootstrap"},
 "mimir.acp.bootstrap": {"mimir.acp.profiles", "mimir.acp.credentials", "mimir.acp.proxy", "mimir.acp.ssh", "mimir.acp.relay"},
 "mimir.acp.host": {"mimir.acp.transport"},
 "mimir.acp.profiles": set(), "mimir.acp.credentials": set(), "mimir.acp.transport": set(),
}
ROOTS = {
 "local": {"mimir.acp.proxy"}, "remote": {"mimir.acp.ssh"}, "relay": {"mimir.acp.relay"},
 "client": {"mimir.acp.__main__", "mimir.acp.bootstrap", "mimir.acp.host"},
}
EXPECTED = {
 "local": {"mimir.acp.proxy","mimir.acp.profiles","mimir.acp.credentials","mimir.acp.transport"},
 "remote": {"mimir.acp.ssh","mimir.acp.proxy","mimir.acp.profiles","mimir.acp.credentials","mimir.acp.transport"},
 "relay": {"mimir.acp.relay","mimir.acp.transport"},
 "client": set(POLICY),
}
FORBIDDEN = ("mimir.runtime", "mimir.server", "mimir.acp.composition", "mimir.agent", "mimir.dispatcher", "mimir.scheduler", "mimir.channel_registry")

def module_paths(package_root: Path) -> dict[str, Path]:
 return {"mimir.acp."+("__main__" if p.name=="__main__.py" else p.stem):p for p in (package_root/"acp").glob("*.py")}

def imports(module: str, path: Path) -> set[str]:
 tree=ast.parse(path.read_text("utf-8")); result=set(); package=module.rsplit(".",1)[0]
 for node in ast.walk(tree):
  if isinstance(node,ast.Import): result.update(alias.name for alias in node.names if alias.name.startswith("mimir."))
  elif isinstance(node,ast.ImportFrom):
   if node.level:
    parts=package.split("."); base=".".join(parts[:len(parts)-node.level+1]); target=(base+"."+node.module) if node.module else base
   else: target=node.module or ""
   if target.startswith("mimir."):
    if target=="mimir.acp": result.update(target+"."+alias.name for alias in node.names)
    else: result.add(target)
 return result

def assert_policy(paths: Mapping[str,Path]) -> None:
 for module, expected in POLICY.items():
  if module not in paths: raise AssertionError(f"missing module {module}")
  actual=imports(module,paths[module]); actual={name for name in actual if name.startswith("mimir.")}
  if actual != expected: raise AssertionError(f"{module} imports {sorted(actual)}, expected {sorted(expected)}")
  source=paths[module].read_text("utf-8")
  if any(name in source for name in FORBIDDEN): raise AssertionError(f"forbidden dependency in {module}")
  tree=ast.parse(source)
  for node in ast.walk(tree):
   if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr=="aclose": raise AssertionError(f"forbidden closer in {module}")
 for name,roots in ROOTS.items():
  reached=set(roots); pending=list(roots)
  while pending:
   current=pending.pop()
   for dependency in POLICY[current]:
    if dependency not in reached: reached.add(dependency); pending.append(dependency)
  if reached != EXPECTED[name]: raise AssertionError(f"{name} closure mismatch")
