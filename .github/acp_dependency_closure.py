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
    "mimir.acp.profiles": set(),
    "mimir.acp.credentials": set(),
    "mimir.acp.transport": set(),
}
ROOTS = {
    "local": {"mimir.acp.proxy"},
    "remote": {"mimir.acp.ssh"},
    "relay": {"mimir.acp.relay"},
    "client": {"mimir.acp.__main__", "mimir.acp.bootstrap", "mimir.acp.host"},
}
EXPECTED = {
    "local": {"mimir.acp.proxy", "mimir.acp.profiles", "mimir.acp.credentials", "mimir.acp.transport"},
    "remote": {"mimir.acp.ssh", "mimir.acp.proxy", "mimir.acp.profiles", "mimir.acp.credentials", "mimir.acp.transport"},
    "relay": {"mimir.acp.relay", "mimir.acp.transport"},
    "client": set(POLICY),
}
FORBIDDEN_MODULES = {
    "mimir.runtime",
    "mimir.server",
    "mimir.acp.composition",
    "mimir.agent",
    "mimir.dispatcher",
    "mimir.scheduler",
    "mimir.channel_registry",
}
FORBIDDEN_CALLS = {
    "mimir.runtime.create_core_services",
    "mimir.runtime.create_agent_runtime",
    "mimir.runtime.CoreServices",
    "mimir.runtime.RuntimeAdapters",
    "mimir.runtime.AgentRuntimeBundle",
    "mimir.runtime.AgentRuntimeBundle.aclose",
    "mimir.runtime._close_bundle",
}


def module_paths(package_root: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for path in package_root.rglob("*.py"):
        relative = path.relative_to(package_root).with_suffix("")
        parts = [package_root.name, *relative.parts]
        if parts[-1] == "__init__":
            parts.pop()
        result[".".join(parts)] = path
    return result


def _resolve_from(module: str, node: ast.ImportFrom) -> str:
    if not node.level:
        return node.module or ""
    package = module.split(".")[:-1]
    base = package[: len(package) - node.level + 1]
    if node.module:
        base.extend(node.module.split("."))
    return ".".join(base)


class Analysis(ast.NodeVisitor):
    def __init__(self, module: str, known_modules: set[str]) -> None:
        self.module = module
        self.known_modules = known_modules
        self.imports: set[str] = set()
        self.aliases: dict[str, str] = {}
        self.calls: set[str] = set()
        self.attributes: set[str] = set()
        self.dynamic_first_party = False

    def _name(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return self.aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            parent = self._name(node.value)
            return f"{parent}.{node.attr}" if parent else None
        return None

    def visit_Import(self, node: ast.Import) -> None:
        for item in node.names:
            local = item.asname or item.name.split(".")[0]
            self.aliases[local] = item.name if item.asname else local
            if item.name == "mimir" or item.name.startswith("mimir."):
                self.imports.add(item.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = _resolve_from(self.module, node)
        for item in node.names:
            if item.name == "*":
                raise AssertionError(f"wildcard import in {self.module}")
            target = f"{base}.{item.name}" if base else item.name
            local = item.asname or item.name
            self.aliases[local] = target
            if target in self.known_modules:
                self.imports.add(target)
            elif base == "mimir" or base.startswith("mimir."):
                self.imports.add(base)

    def visit_Assign(self, node: ast.Assign) -> None:
        value = self._name(node.value)
        if value:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.aliases[target.id] = value
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        value = self._name(node.value) if node.value else None
        if value and isinstance(node.target, ast.Name):
            self.aliases[node.target.id] = value
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = self._name(node.func)
        if name:
            self.calls.add(name)
            if name in {"importlib.import_module", "__import__"}:
                if not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
                    self.dynamic_first_party = True
                elif node.args[0].value == "mimir" or node.args[0].value.startswith("mimir."):
                    self.dynamic_first_party = True
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        name = self._name(node)
        if name:
            self.attributes.add(name)
        self.generic_visit(node)


def analyze(module: str, path: Path, known_modules: set[str]) -> Analysis:
    analysis = Analysis(module, known_modules)
    analysis.visit(ast.parse(path.read_text("utf-8"), filename=str(path)))
    return analysis


def _first_party_imports(analysis: Analysis, known_modules: set[str]) -> set[str]:
    result: set[str] = set()
    for imported in analysis.imports:
        if imported in known_modules:
            result.add(imported)
            continue
        candidates = [name for name in known_modules if name.startswith(imported + ".")]
        if candidates:
            result.add(imported)
        elif imported == "mimir" or imported.startswith("mimir."):
            result.add(imported)
    return result


def closure(paths: Mapping[str, Path], roots: set[str]) -> tuple[set[str], dict[str, Analysis]]:
    known = set(paths)
    reached: set[str] = set()
    analyses: dict[str, Analysis] = {}
    pending = list(roots)
    while pending:
        module = pending.pop()
        if module in reached:
            continue
        if module not in paths:
            raise AssertionError(f"unresolved first-party module {module}")
        reached.add(module)
        analysis = analyze(module, paths[module], known)
        analyses[module] = analysis
        if analysis.dynamic_first_party:
            raise AssertionError(f"unresolved dynamic first-party import in {module}")
        _assert_sinks(module, analysis)
        for dependency in _first_party_imports(analysis, known):
            if dependency not in known:
                raise AssertionError(f"unresolved first-party import {dependency} in {module}")
            pending.append(dependency)
    return reached, analyses


def _assert_sinks(module: str, analysis: Analysis) -> None:
    names = analysis.calls | analysis.attributes | set(analysis.aliases.values())
    for name in names:
        if name in FORBIDDEN_CALLS or any(name == item or name.startswith(item + ".") for item in FORBIDDEN_MODULES):
            raise AssertionError(f"forbidden dependency {name} in {module}")
    if any(name.endswith(".aclose") for name in analysis.calls):
        raise AssertionError(f"forbidden closer in {module}")


def _assert_analysis(module: str, analysis: Analysis, paths: Mapping[str, Path]) -> None:
    actual = _first_party_imports(analysis, set(paths))
    if actual != POLICY[module]:
        raise AssertionError(f"{module} imports {sorted(actual)}, expected {sorted(POLICY[module])}")
    _assert_sinks(module, analysis)


def assert_policy(paths: Mapping[str, Path]) -> None:
    for name, roots in ROOTS.items():
        reached, analyses = closure(paths, roots)
        for current, analysis in analyses.items():
            _assert_analysis(current, analysis, paths)
        if reached != EXPECTED[name]:
            raise AssertionError(f"{name} closure {sorted(reached)}, expected {sorted(EXPECTED[name])}")
