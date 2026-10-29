"""Reviewed disk-write sites in bundled optional-skill scripts.

This is a source inventory, not a general Python data-flow analysis or a ban on
content stores. In particular Gmail's existing audit retains headers, answers,
and model, and CI-watch retains log tails. New sites require explicit review.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest


# Relative script, enclosing function/class, complete normalized call expression.
# Keep multiplicity: a set of target variable names hides added duplicate writes
# and conflates unrelated functions (e.g. dispatcher's two different ledgers).
WriteSite = tuple[str, str, str]


def _write_open(node: ast.Call, mode_position: int) -> bool:
    """Ignore only demonstrably read-only opens; unknown modes fail closed."""
    expression = ast.unparse(node)
    assert not any(isinstance(arg, ast.Starred) for arg in node.args), expression
    assert not any(kw.arg is None for kw in node.keywords), expression
    mode = next((kw.value for kw in node.keywords if kw.arg == "mode"), None)
    if mode is None and len(node.args) > mode_position:
        mode = node.args[mode_position]
    if mode is None:
        return False  # open/Path.open default to "r".
    assert isinstance(mode, ast.Constant) and isinstance(mode.value, (str, bytes)), (
        f"Unreviewable open mode: {expression}"
    )
    value = mode.value
    # Binary modes (wb, rb+, etc.) and even byte-literal spellings must not
    # disappear from the inventory; this scanner does not execute the call.
    if isinstance(value, bytes):
        value = value.decode("ascii")
    assert (
        value
        and all(flag in "rwaxbt+" for flag in value)
        and sum(value.count(flag) for flag in "rwax") == 1
        and value.count("b") + value.count("t") <= 1
        and value.count("+") <= 1
    ), f"Unknown open mode: {expression}"
    return any(flag in value for flag in "wax+")


def _script_write_sites(source: str, script: str) -> Counter[WriteSite]:
    tree = ast.parse(source, filename=script)
    sites: Counter[WriteSite] = Counter()

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scope: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        visit_AsyncFunctionDef = visit_FunctionDef
        visit_ClassDef = visit_FunctionDef

        def visit_Call(self, node: ast.Call) -> None:
            call = ast.unparse(node.func)
            writer = False
            if call in {"open", "builtins.open", "io.open"}:
                writer = _write_open(node, 1)
            elif call == "os.open":
                # Include descriptor opens conservatively, including reviewed
                # O_RDONLY directory fsync handles. Flags are not string modes.
                writer = True
            elif call.endswith(".open"):
                # Also support the unbound Path.open(path, mode) spelling.
                position = 1 if call in {"Path.open", "pathlib.Path.open"} else 0
                writer = _write_open(node, position)
            elif call.endswith((".write_text", ".write_bytes")):
                writer = True
            elif call == "os.replace":
                writer = True
            elif call.endswith(".replace"):
                # Path.replace takes one target (positional or keyword).
                # str.replace takes >=2 arguments; datetime.replace uses date
                # field keywords. Do not restrict Path receivers to tmp/temp.
                writer = len(node.args) == 1 or any(
                    kw.arg == "target" for kw in node.keywords
                )
            elif call in {"tempfile.NamedTemporaryFile", "NamedTemporaryFile"}:
                # Creation is a write even with a read mode. The full expression
                # records dir/prefix/suffix and handles the default temp dir.
                writer = True
            if writer:
                sites[(script, ".".join(self.scope) or "<module>", ast.unparse(node))] += 1
            self.generic_visit(node)

    Visitor().visit(tree)
    return sites


def _bundled_write_sites(root: Path) -> Counter[WriteSite]:
    sites: Counter[WriteSite] = Counter()
    for scripts in sorted(root.glob("*/scripts")):
        for script in sorted(scripts.rglob("*.py")):
            relative = script.relative_to(root)
            if "tests" in relative.parts or script.name.startswith("test_"):
                continue
            sites.update(
                _script_write_sites(script.read_text(encoding="utf-8"), relative.as_posix())
            )
    return sites


def _assert_reviewed(sites: Counter[WriteSite], reviewed: Counter[WriteSite]) -> None:
    assert sites == reviewed, (
        f"Unreviewed write sites: {sites - reviewed}\n"
        f"Missing reviewed write sites: {reviewed - sites}"
    )


def test_bundled_script_write_sites_match_reviewed_inventory() -> None:
    repo = Path(__file__).resolve().parents[1]
    # Cultivate ships the GitHub pollers; both bundled trees share one inventory.
    roots = (
        repo / "mimir" / "optional-skills",
        repo / "plugins" / "cultivate" / "mimir_cultivate" / "defaults" / "skills",
    )
    # These are actual reviewed call expressions, not a per-skill target allowlist.
    # Do not narrow production audit payloads to make this inventory pass.
    reviewed = {
        "dependency-advisory-watch/scripts/poller.py": [
            ("_write_cursor", "temp.write_text(content, encoding='utf-8')"),  # advisory IDs
            ("_write_cursor", "temp.replace(path)"),
            ("_seed_state_gitignore", "gitignore.write_text(_STATE_GITIGNORE, encoding='utf-8')"),
        ],
        "github-ci-watch/scripts/poller.py": [
            ("_save_seen", "tmp.open('w', encoding='utf-8')"),  # run IDs/watermarks
            ("_save_seen", "os.replace(tmp, SEEN_FILE)"),
            ("_save_seen", "os.open(STATE_DIR, os.O_RDONLY)"),  # directory fsync
            # Existing bounded CI log tails under STATE_DIR/logs (not turn scratch).
            ("_job_log", "tempfile.NamedTemporaryFile(dir=logs, delete=False)"),
            ("_job_log", "os.replace(tmp, path)"),
            ("_seed_state_gitignore", "gi.write_text(_STATE_GITIGNORE, encoding='utf-8')"),
        ],
        "github-poller/scripts/poller.py": [
            ("_save_cursor", "tmp.open('w', encoding='utf-8')"),
            ("_save_cursor", "os.replace(tmp, CURSOR_FILE)"),
            ("_save_cursor", "os.open(STATE_DIR, os.O_RDONLY)"),
            # Empty per-delivery-key claim file; no PR content.
            ("_claim_delivery", "os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 384)"),
            ("_seed_state_gitignore", "gi.write_text(_STATE_GITIGNORE, encoding='utf-8')"),
        ],
        "tracker-poller/scripts/poller.py": [
            ("_save_cursor", "tmp.open('w', encoding='utf-8')"),
            ("_save_cursor", "os.replace(tmp, CURSOR_FILE)"),
            ("_save_cursor", "os.open(STATE_DIR, os.O_RDONLY)"),
            ("_seed_state_gitignore", "gi.write_text(_STATE_GITIGNORE, encoding='utf-8')"),
        ],
        "gmail-poller/scripts/poller.py": [
            ("_save_cursor", "tmp.write_text(json.dumps(ids), encoding='utf-8')"),
            ("_save_cursor", "tmp.replace(CURSOR_FILE)"),
            # Full existing message_id/url/from/subject/answers/model audit,
            # with optional shadow/mode. This is NOT an ID-only cursor.
            ("_audit_drop", "TRIAGE_DROPPED_FILE.open('a', encoding='utf-8')"),
            ("_seed_state_gitignore", "gi.write_text(_STATE_GITIGNORE, encoding='utf-8')"),
        ],
        "social-cli/scripts/poller.py": [
            ("_save_cursor", "tmp.write_text(json.dumps(ids), encoding='utf-8')"),
            ("_save_cursor", "tmp.replace(CURSOR_FILE)"),
            ("_seed_state_gitignore", "gi.write_text(_STATE_GITIGNORE, encoding='utf-8')"),
        ],
        "social-cli/scripts/feed_poller.py": [
            ("_save_cursor", "tmp.write_text(json.dumps(ids), encoding='utf-8')"),
            ("_save_cursor", "tmp.replace(CURSOR_FILE)"),
            ("_seed_state_gitignore", "gi.write_text(_STATE_GITIGNORE, encoding='utf-8')"),
        ],
        "social-cli/scripts/dispatcher.py": [
            # state_dir/unrecognized-outbox-ledger.jsonl: relative path + hash.
            ("_flag_unrecognized", "ledger.open('a+', encoding='utf-8')"),
            # state_dir/dispatched-ledger.jsonl: hash/path/day/post reservations.
            ("dispatch_merged", "ledger.open('a+', encoding='utf-8')"),
            # Private TemporaryDirectory snapshot of the verified outbox YAML.
            ("dispatch_merged", "snapshot.open('x', encoding='utf-8', newline='')"),
        ],
    }
    expected = Counter(
        (script, scope, call)
        for script, entries in reviewed.items()
        for scope, call in entries
    )
    _assert_reviewed(sum((_bundled_write_sites(root) for root in roots), Counter()), expected)


@pytest.mark.parametrize(
    "mode",
    ["w", "a", "x", "r+", "w+", "a+", "x+", "wb", "ab", "xb", "rb+", "w+b", b"wb"],
)
@pytest.mark.parametrize(
    "template",
    [
        "open(target, {mode})",
        "open(file=target, mode={mode})",
        "builtins.open(target, mode={mode})",
        "target.open({mode})",
        "target.open(mode={mode})",
        "Path.open(target, {mode})",
    ],
)
def test_write_open_modes_are_discovered(mode: str | bytes, template: str) -> None:
    expression = template.format(mode=repr(mode))
    assert _script_write_sites(expression, "synthetic.py") == Counter({
        ("synthetic.py", "<module>", ast.unparse(ast.parse(expression).body[0].value)): 1,
    })


@pytest.mark.parametrize(
    "expression",
    ["open(target)", "open(target, 'r')", "open(target, mode='rb')", "target.open()", "target.open(mode='rt')"],
)
def test_read_only_opens_are_not_writes(expression: str) -> None:
    assert not _script_write_sites(expression, "synthetic.py")


@pytest.mark.parametrize(
    "expression",
    ["open(target, mode)", "open(target, mode=mode)", "target.open(mode)", "target.open(mode=mode)",
     "open(target, **options)", "target.open(*options)", "open(target, 'unknown')"],
)
def test_unknown_open_modes_cannot_silently_escape_inventory(expression: str) -> None:
    with pytest.raises(AssertionError):
        _script_write_sites(expression, "synthetic.py")


@pytest.mark.parametrize(
    "expression",
    ["target.write_text(text)", "target.write_bytes(data)", "os.replace(src, dst)",
     "staging.replace(destination)", "staging.replace(target=destination)",
     "tempfile.NamedTemporaryFile()", "tempfile.NamedTemporaryFile(dir=logs, mode='wb')"],
)
def test_other_write_apis_are_discovered(expression: str) -> None:
    assert sum(_script_write_sites(expression, "synthetic.py").values()) == 1


def test_builtin_open_mutation_requires_review(tmp_path: Path) -> None:
    script = tmp_path / "example" / "scripts" / "poller.py"
    script.parent.mkdir(parents=True)
    script.write_text("open('cursor.json', 'r')\n", encoding="utf-8")
    baseline = _bundled_write_sites(tmp_path)
    assert not baseline
    script.write_text("open('cursor.json', 'r')\nopen('content.jsonl', 'w')\n", encoding="utf-8")
    mutated = _bundled_write_sites(tmp_path)
    assert mutated == Counter({
        ("example/scripts/poller.py", "<module>", "open('content.jsonl', 'w')"): 1,
    })
    with pytest.raises(AssertionError, match="Unreviewed write sites"):
        _assert_reviewed(mutated, baseline)


def test_alternate_and_nested_scripts_are_scanned_but_tests_are_not(tmp_path: Path) -> None:
    scripts = tmp_path / "example" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "poller.py").write_text("pass\n", encoding="utf-8")
    (scripts / "feed_poller.py").write_text("path.write_bytes(data)\n", encoding="utf-8")
    (scripts / "helpers").mkdir()
    (scripts / "helpers" / "writer.py").write_text("open(target, mode='ab')\n", encoding="utf-8")
    for directory in (tmp_path / "example" / "tests", scripts / "tests"):
        directory.mkdir()
        (directory / "helper.py").write_text("path.write_text(content)\n", encoding="utf-8")
    (scripts / "test_writer.py").write_text("open(target, 'w')\n", encoding="utf-8")
    observed = _bundled_write_sites(tmp_path)
    assert observed == Counter({
        ("example/scripts/feed_poller.py", "<module>", "path.write_bytes(data)"): 1,
        ("example/scripts/helpers/writer.py", "<module>", "open(target, mode='ab')"): 1,
    })
    with pytest.raises(AssertionError, match="Unreviewed write sites"):
        _assert_reviewed(observed, Counter())


def test_repeated_write_sites_are_not_collapsed() -> None:
    expression = "open(target, 'w')"
    assert _script_write_sites(f"{expression}\n{expression}\n", "synthetic.py") == Counter({
        ("synthetic.py", "<module>", expression): 2,
    })


def test_inventory_distinguishes_same_target_in_different_functions() -> None:
    source = "def first():\n    path.write_text(text)\ndef second():\n    path.write_text(text)\n"
    assert _script_write_sites(source, "synthetic.py") == Counter({
        ("synthetic.py", "first", "path.write_text(text)"): 1,
        ("synthetic.py", "second", "path.write_text(text)"): 1,
    })
