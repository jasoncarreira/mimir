"""Inventory of bundled poller disk writes (new content stores need review)."""

from __future__ import annotations

import ast
from pathlib import Path


def test_bundled_poller_write_targets_are_only_cursors_or_known_temporary_tails() -> None:
    root = Path(__file__).resolve().parents[1] / "mimir" / "optional-skills"
    observed: set[tuple[str, str]] = set()
    for script in root.glob("*/scripts/poller.py"):
        tree = ast.parse(script.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            call = ast.unparse(node.func)
            if call.endswith((".write_text", ".write_bytes")):
                target = call.rsplit(".", 1)[0]
            elif call.endswith(".open") and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value in {"w", "a", "wb", "ab"}:
                target = call.rsplit(".", 1)[0]
            elif call in {"os.replace", "temp.replace", "tmp.replace"}:
                target = ast.unparse(node.args[-1])
            elif call == "tempfile.NamedTemporaryFile":
                target = "NamedTemporaryFile:" + ast.unparse(next(kw.value for kw in node.keywords if kw.arg == "dir"))
            else:
                continue
            observed.add((script.parent.parent.name, target))
    assert observed == {
        ("dependency-advisory-watch", "temp"),  # advisory-id cursor temp
        ("dependency-advisory-watch", "path"),
        ("dependency-advisory-watch", "gitignore"),
        ("github-ci-watch", "tmp"),  # seen-run-id cursor temp
        ("github-ci-watch", "SEEN_FILE"),
        ("github-ci-watch", "NamedTemporaryFile:logs"),  # moved to turn scratch
        ("github-ci-watch", "path"),
        ("github-ci-watch", "gi"),
        ("github-poller", "tmp"),  # id cursor temp
        ("github-poller", "CURSOR_FILE"),
        ("github-poller", "gi"),
        ("gmail-poller", "tmp"),  # id cursor temp
        ("gmail-poller", "CURSOR_FILE"),
        ("gmail-poller", "TRIAGE_DROPPED_FILE"),  # id + numeric decision only
        ("gmail-poller", "gi"),
        ("social-cli", "tmp"),  # id cursor temp
        ("social-cli", "CURSOR_FILE"),
        ("social-cli", "gi"),
    }
