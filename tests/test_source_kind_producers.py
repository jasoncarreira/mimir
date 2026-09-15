"""Static first-party producer audit (#1756), not runtime label validation."""

from __future__ import annotations

import ast
from pathlib import Path
from textwrap import dedent

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _audit(sources: dict[str, str], members: set[str]) -> tuple[list[str], set[str]]:
    """Follow producer values backwards, including wrapper defaults and callers.

    This is deliberately a conservative source audit, not a Python evaluator:
    unsupported value expressions fail closed. Persisted from_record data is
    not a first-party producer. Call targets, never keyword spelling alone,
    seed the traversal (archive_factory_record is consequently irrelevant).
    """
    trees = {path: ast.parse(text, filename=path) for path, text in sources.items()}
    parents = {}
    paths = {}
    aliases = {}
    calls = []
    for path, tree in trees.items():
        aliases[path] = {}
        for node in ast.walk(tree):
            paths[node] = path
            for child in ast.iter_child_nodes(node):
                parents[child] = node
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    aliases[path][alias.asname or alias.name] = alias.name
            if isinstance(node, ast.Call):
                calls.append(node)

    def scope(node):
        node = parents.get(node)
        while node is not None:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module, ast.ClassDef)):
                return node
            node = parents.get(node)

    def target(node):
        if isinstance(node, ast.Name):
            return aliases[paths[node]].get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return f"{target(node.value)}.{node.attr}"
        return ""

    def producer(call):
        name = target(call.func)
        parts = name.split(".")
        if parts[-1] in {"SourceLabel", "prompt_source_label"}:
            return parts[-1]
        if len(parts) >= 2 and parts[-2] == "SourceLabel" and parts[-1] in {"for_service", "derived"}:
            return ".".join(parts[-2:])
        owner = scope(call)
        if name == "cls" and getattr(owner, "name", "") in {"for_service", "derived"}:
            if getattr(scope(owner), "name", "") == "SourceLabel":
                return f"SourceLabel.{owner.name}"
        return None

    errors = []
    audited = set()
    visited = set()

    def error(node, message):
        errors.append(f"{paths[node]}:{node.lineno}: {message}")

    def check(value):
        if value in visited:
            return
        visited.add(value)
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            if value.value not in members:
                error(value, f"nonmember source_kind {value.value!r}")
        elif isinstance(value, ast.Attribute) and target(value.value).split(".")[-1] == "SourceKind":
            # Member names are checked separately from their serialized values.
            if value.attr not in enum_names:
                error(value, f"unknown SourceKind member {value.attr}")
        elif isinstance(value, ast.IfExp):
            check(value.body)
            check(value.orelse)
        elif (
            isinstance(value, ast.Call)
            and paths[value] == "mimir/poller_recovery.py"
            and getattr(scope(value), "name", "") == "_event_from_stash"
            and target(value.func) == "source.get"
            and len(value.args) == 2
            and isinstance(value.args[0], ast.Constant)
            and value.args[0].value == "source_kind"
        ):
            # Original main decodes persisted labels inline, before from_record
            # replaced this constructor. Only its fallback mints a kind.
            check(value.args[1])
        elif isinstance(value, ast.Name):
            owner = scope(value)
            found = False
            while owner is not None:
                for node in ast.walk(owner):
                    if scope(node) is not owner:
                        continue
                    if isinstance(node, ast.Assign):
                        names = node.targets
                    elif isinstance(node, ast.AnnAssign):
                        names = [node.target]
                    else:
                        continue
                    if any(isinstance(name, ast.Name) and name.id == value.id for name in names):
                        if node.value is not None:
                            found = True
                            check(node.value)
                if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    positional = owner.args.posonlyargs + owner.args.args
                    parameters = positional + owner.args.kwonlyargs
                    if any(arg.arg == value.id for arg in parameters):
                        found = True
                        audited.add(owner.name)
                        defaults = dict(zip(
                            [arg.arg for arg in positional[-len(owner.args.defaults):]],
                            owner.args.defaults,
                        ))
                        defaults.update(zip(
                            [arg.arg for arg in owner.args.kwonlyargs], owner.args.kw_defaults,
                        ))
                        if defaults.get(value.id) is not None:
                            check(defaults[value.id])
                        for call in calls:
                            if target(call.func).split(".")[-1] != owner.name:
                                continue
                            arguments(call, value.id, positional)
                if found:
                    break
                owner = scope(owner)
            if not found:
                error(value, f"unresolved source_kind name {value.id}")
        else:
            error(value, f"unresolved source_kind expression {ast.unparse(value)}")

    def arguments(call, parameter="source_kind", positional=()):
        for keyword in call.keywords:
            if keyword.arg == parameter:
                check(keyword.value)
            elif keyword.arg is None:
                error(call, "unresolved producer **kwargs")
        for index, arg in enumerate(positional):
            if arg.arg == parameter and index < len(call.args):
                check(call.args[index])

    enum_names = _source_kind_members()[0]
    for call in calls:
        name = producer(call)
        if name:
            audited.add(name)
            # Dataclass field order; classmethod factories have keyword-only kinds.
            positional = [ast.arg(arg=name) for name in (
                "principal", "domain", "resource_id", "bridge_instance",
                "sensitivity", "authorized_principals", "source_kind",
            )] if name == "SourceLabel" else []
            arguments(call, positional=positional)
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.target.id == "source_kind" and getattr(scope(node), "name", "") == "SourceLabel":
                    check(node.value)
    return sorted(set(errors)), audited


def _source_kind_members() -> tuple[set[str], set[str]]:
    tree = ast.parse((ROOT / "mimir/models.py").read_text())
    enum = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "SourceKind")
    values = {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in enum.body if isinstance(node, ast.Assign)
    }
    return set(values), set(values.values())


def test_production_source_kind_producers() -> None:
    sources = {
        str(path.relative_to(ROOT)): path.read_text()
        for path in sorted((ROOT / "mimir").rglob("*.py"))
    }
    errors, audited = _audit(sources, _source_kind_members()[1])
    assert not errors, "\n".join(errors)
    assert {
        "SourceLabel", "SourceLabel.derived", "prompt_source_label",
        "_prompt_source_labels", "_propagate_ifc_labels",
    } <= audited
    assert "archive_factory_record" not in audited


@pytest.mark.parametrize("target", [
    "SourceLabel", "models.SourceLabel", "SourceLabel.for_service",
    "SourceLabel.derived", "prompt_source_label", "prompt_sources.prompt_source_label",
])
def test_bogus_literal_producer_fails(target: str) -> None:
    errors, _ = _audit({"fixture.py": f'{target}(source_kind="bogus")'}, {"channel"})
    assert len(errors) == 1
    assert "nonmember source_kind 'bogus'" in errors[0]


@pytest.mark.parametrize("injection", [
    'def outer(source_kind="bogus"): return wrapper(source_kind)',
    'wrapper("bogus")',
    'wrapper(source_kind="bogus")',
    'def outer(kind="bogus"): return wrapper(kind)\nouter()',
    'kind = "bogus"\nwrapper(kind)',
    'wrapper("channel" if condition else "bogus")',
])
def test_bogus_pass_through_fails(injection: str) -> None:
    source = dedent('''\
        from mimir.models import SourceLabel as Label
        def wrapper(source_kind="channel"):
            return Label(source_kind=source_kind)
    ''') + injection
    errors, audited = _audit({"fixture.py": source}, {"channel"})
    assert any("nonmember source_kind 'bogus'" in error for error in errors)
    assert "wrapper" in audited


@pytest.mark.parametrize("method", ["for_service", "derived"])
def test_factory_default_fails(method: str) -> None:
    source = f'''\
class SourceLabel:
    @classmethod
    def {method}(cls, *, source_kind="bogus"):
        return cls(source_kind=source_kind)
'''
    errors, _ = _audit({"fixture.py": source}, {"channel"})
    assert any("nonmember source_kind 'bogus'" in error for error in errors)


def test_unrelated_kinds_and_persisted_data_are_not_producers() -> None:
    errors, audited = _audit({"fixture.py": dedent('''\
        archive_factory_record(source_kind="operator_command")
        archive_factory_record(source_kind="web_ui")
        SourceLabel.from_record({"source_kind": "legacy_unknown"})
        def archive_factory_record(*, source_kind="not_a_label"):
            return source_kind
    ''')}, {"channel"})
    assert errors == []
    assert audited == set()


def test_unresolved_producer_fails_closed() -> None:
    errors, _ = _audit({"fixture.py": "SourceLabel(source_kind=external())"}, {"channel"})
    assert len(errors) == 1
    assert "unresolved source_kind expression" in errors[0]


@pytest.mark.parametrize("expression", [
    '"bogus"', '"channel" if condition else "bogus"', 'SourceKind.BOGUS',
])
def test_local_assignment_and_constructor_default_fail(expression: str) -> None:
    for source in (
        f"kind = {expression}\nSourceLabel(source_kind=kind)",
        f"class SourceLabel:\n    source_kind: str = {expression}",
        f"def wrapper(source_kind='channel'):\n    source_kind = {expression}\n    return SourceLabel(source_kind=source_kind)",
    ):
        errors, _ = _audit({"fixture.py": source}, {"channel"})
        assert errors


def test_cross_module_pass_through_alias_fails() -> None:
    sources = {
        "producer.py": dedent('''\
            def wrapper(source_kind="channel"):
                return SourceLabel.derived(source_kind=source_kind)
        '''),
        "caller.py": 'from producer import wrapper as forward\nforward("bogus")',
    }
    errors, audited = _audit(sources, {"channel"})
    assert len(errors) == 1
    assert "caller.py:2: nonmember source_kind 'bogus'" == errors[0]
    assert "wrapper" in audited


@pytest.mark.parametrize("fallback", ["channel", "bogus"])
def test_original_main_decoder_still_checks_fallback(fallback: str) -> None:
    source = f'''\
def _event_from_stash(record):
    return SourceLabel(source_kind=source.get("source_kind", "{fallback}"))
'''
    errors, _ = _audit({"mimir/poller_recovery.py": source}, {"channel"})
    assert bool(errors) == (fallback == "bogus")
    # Identically spelled lookups outside the audited decoder are not exempt.
    errors, _ = _audit({"new_producer.py": source}, {"channel"})
    assert errors
