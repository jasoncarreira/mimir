"""Closure and cross-process regression tests for identity state writers."""

from __future__ import annotations

import ast
import select
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from mimir import identities_populator as pop
from mimir import cli
from mimir.commands import identities as identity_cmd
from mimir.commands import setup


def _people(home: Path) -> list[dict]:
    return yaml.safe_load((home / "state" / "identities.yaml").read_text())["people"]


_CLI_SCRIPT = """
import sys
from pathlib import Path
from mimir.cli import main
from mimir import identities_populator as pop

home, actor, operation = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
original = pop._load_yaml
def load(path):
    result = original(path)
    print('loaded', flush=True)
    if actor == 'A':
        sys.stdin.readline()
    return result
pop._load_yaml = load
print('started', flush=True)
if actor == 'B' and operation != 'two-adds':
    pop.capture_dm_channel(home, 'slack-U2', 'slack', 'dm-slack-D2')
elif operation.startswith('remove:'):
    options = ['--canonical', 'alice'] if operation == 'remove:canonical' else ['--alias', 'slack-U1']
    main(['identities', 'remove', '--home', str(home), *options])
else:
    alias = 'slack-U1' if actor == 'A' else 'slack-U3'
    main(['identities', 'add', '--home', str(home), '--canonical', 'alice', '--alias', alias])
"""


@pytest.mark.parametrize("operation", ["add", "remove:canonical", "remove:alias_last", "remove:alias_keep", "two-adds"])
def test_cli_transactions_serialize_across_processes(tmp_path: Path, operation: str) -> None:
    # Both children load the same identities file. B must not read until A's
    # transaction commits, including the last-alias and surviving-alias branches.
    pop.add_identity_alias(tmp_path, "alice", "slack-U1")
    if operation == "remove:alias_keep":
        pop.add_identity_alias(tmp_path, "alice", "discord-1")

    def spawn(actor: str) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, "-c", _CLI_SCRIPT, str(tmp_path), actor, operation],
            stdin=subprocess.PIPE if actor == "A" else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
        )

    first = spawn("A")
    second = None
    try:
        assert select.select([first.stdout], [], [], 15)[0]
        assert first.stdout.readline().strip() == b"started"
        assert select.select([first.stdout], [], [], 15)[0]
        assert first.stdout.readline().strip() == b"loaded"
        second = spawn("B")
        assert select.select([second.stdout], [], [], 15)[0]
        assert second.stdout.readline().strip() == b"started"
        assert not select.select([second.stdout], [], [], 0.2)[0], "B loaded before A committed"
        _, error = first.communicate(b"continue\n", timeout=15)
        assert first.returncode == 0, error
        output, error = second.communicate(timeout=15)
        assert second.returncode == 0, error
        assert b"loaded" in output
        people = _people(tmp_path)
        if operation == "two-adds":
            assert {a for p in people for a in p["aliases"]} == {"slack-U1", "slack-U3"}
        else:
            assert any(p.get("dm_channels", {}).get("slack") == "dm-slack-D2" for p in people)
            if operation == "add":
                assert "slack-U1" in people[0]["aliases"]
            elif operation == "remove:alias_keep":
                assert people[0]["aliases"] == ["discord-1"]
            else:
                assert all(p["canonical"] != "alice" for p in people)
        assert not (tmp_path / "state" / "identities.yaml.tmp").exists()
    finally:
        for child in (first, second):
            if child is not None and child.poll() is None:
                child.kill()
                child.communicate(timeout=5)


def test_setup_seed_does_not_replace_file_created_after_existence_check(tmp_path, monkeypatch):
    path = tmp_path / "state" / "identities.yaml"
    original_link = setup.os.link
    original_replace = setup.os.replace
    existing = "people:\n- canonical: preserved\n  aliases: [slack-U9]\n"

    def racing_link(source, dest, **kwargs):
        path.write_text(existing)
        return original_link(source, dest, **kwargs)

    def racing_replace(source, dest, **kwargs):
        path.write_text(existing)
        return original_replace(source, dest, **kwargs)

    monkeypatch.setattr(setup.os, "link", racing_link)
    monkeypatch.setattr(setup.os, "replace", racing_replace)
    try:
        assert setup._seed_identities(tmp_path, setup.DEFAULT_IDENTITIES_YAML) is False
    finally:
        assert path.read_text() == existing
    assert not list(path.parent.glob(".identities-seed-*.tmp"))


def test_nested_identity_transaction_reuses_home_lock(tmp_path):
    @pop._serialized_identities_write
    def nested(home):
        pop.add_identity_alias(home, "alice", "slack-U1")

    nested(tmp_path)
    assert _people(tmp_path)[0]["aliases"] == ["slack-U1"]
    assert (tmp_path / "state" / "identities.lock").exists()
    assert not (tmp_path / "state" / "state" / "identities.lock").exists()


# Each function that can reach the final identities write must be reviewed here.
_ALLOWED = {
    "identities_populator.py": {
        "_atomic_write_identities", "add_identity_alias", "remove_identity",
        "issue_web_key", "revoke_web_key", "set_user_prefs", "capture_dm_channel",
        "request_pairing_with_code", "prepare_pairing_code_delivery",
        "approve_pairing", "approve_pairing_code", "merge_into_yaml",
    },
    "commands/setup.py": {"_seed_identities"},
}
_SINKS = {"write_text", "write_bytes", "open", "rename", "replace", "unlink",
          "safe_dump", "_atomic_write_identities", "_write_if_missing",
          "write_framework_file", "publish_framework_files", "link"}


def _identity_writes(source: str) -> list[tuple[str, int]]:
    """Follow local aliases of an identities path into file-writing calls."""
    tree = ast.parse(source)
    found = []
    for fn in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
        # Skip nested functions here: the enclosing seed owns its publish closure.
        aliases = {"yaml_path"} if fn.name == "_atomic_write_identities" else set()
        calls = list(ast.walk(fn))
        for node in calls:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                if value is not None and any(
                    isinstance(n, ast.Constant) and n.value == "identities.yaml"
                    for n in ast.walk(value)
                ):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    aliases.update(n.id for target in targets for n in ast.walk(target) if isinstance(n, ast.Name))
        for node in calls:
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if name not in _SINKS:
                continue
            # safe_dump in the same function as the identities path is also a sink.
            args = ([node.func.value] if isinstance(node.func, ast.Attribute) else []) + list(node.args)
            if (name == "safe_dump" and aliases) or any(
                (isinstance(n, ast.Name) and n.id in aliases)
                or (isinstance(n, ast.Constant) and n.value == "identities.yaml")
                for arg in args for n in ast.walk(arg)
            ):
                found.append((fn.name, node.lineno))
    return found


def test_only_allowlisted_functions_write_identities_yaml():
    root = Path(__file__).resolve().parents[1] / "mimir"
    violations = []
    seen: dict[str, set[str]] = {}
    for path in root.rglob("*.py"):
        relative = path.relative_to(root).as_posix()
        for fn, line in _identity_writes(path.read_text(encoding="utf-8")):
            seen.setdefault(relative, set()).add(fn)
            if fn not in _ALLOWED.get(relative, set()):
                violations.append(f"{relative}:{line}: {fn} writes identities.yaml")
    assert not violations, "\n".join(violations)
    assert seen == _ALLOWED, f"identities.yaml writer allowlist drift: {seen!r}"


def test_identity_writer_scan_reports_direct_path_writes():
    source = '''
from pathlib import Path
def unrelated(home):
    (Path(home) / "state" / "identities.yaml").write_text("lost")
'''
    assert _identity_writes(source) == [("unrelated", 4)]


def test_cli_uses_shared_unique_temp_writer_only():
    assert not hasattr(identity_cmd, "_identities_save")
    assert not hasattr(cli, "_identities_save")
    source = (Path(__file__).resolve().parents[1] / "mimir" / "identities_populator.py").read_text()
    writer = next(
        n for n in ast.parse(source).body
        if isinstance(n, ast.FunctionDef) and n.name == "_atomic_write_identities"
    )
    calls = [n for n in ast.walk(writer) if isinstance(n, ast.Call)]
    assert any(isinstance(n.func, ast.Attribute) and n.func.attr == "mkstemp" for n in calls)
    assert any(isinstance(n.func, ast.Attribute) and n.func.attr == "replace" for n in calls)
    assert not any(
        isinstance(n, ast.Constant) and n.value == ".yaml.tmp"
        for n in ast.walk(writer)
    )
