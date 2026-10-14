"""Static audit of destinations advertised to always-tainted poller turns.

The poller subprocess may write its own cursor to STATE_DIR. This audit concerns
*agent-facing* write instructions, not subprocess I/O. Dynamic destinations
should be declared in ``agent_write_paths`` in pollers.json so they can be
checked too. The veto itself remains in access_control.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from .access_control import tainted_file_write_target

_PATH = re.compile(r"(?:\$\{?STATE_DIR\}?|(?:\$\{?MIMIR_HOME\}?/)?(?:state|memory|scratch)/)[\w./${}*-]*")
_WRITE = re.compile(r"\b(?:write|save|create|put|draft|note|outbox|persist|append|edit|add)\b", re.I)


def refused_poller_write_paths(
    skill_dir: Path, entry: dict, home: Path,
) -> tuple[str, ...]:
    """Report agent-directed file destinations refused by the tainted-write veto.

    Inspect literal prompt fragments in the script and declared destinations;
    a mention that explicitly locates a file *in a proposal worktree* is not a
    live-home write. STATE_DIR in a prompt denotes live poller state, unlike
    subprocess cursor writes which are outside this agent-facing check.
    """
    name = entry.get("name", "")
    paths: set[str] = set()
    fragments: list[str] = []
    command = entry.get("command", "")
    # Poller commands normally name scripts/poller.py. Do not execute or import
    # home-installed scripts during discovery.
    for relative in re.findall(r"scripts/[\w.-]+\.py", command):
        script = skill_dir / relative
        try:
            tree = ast.parse(script.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeError):
            continue
        aliases: dict[str, str] = {"STATE_DIR": "$STATE_DIR"}
        # Common pattern: DRAFTS_DIR = STATE_DIR / "drafts", then an
        # f-string prompt interpolates {DRAFTS_DIR}. Resolve only simple
        # lexical joins, never evaluate poller code at discovery time.
        formatted_parts = {
            id(part) for node in ast.walk(tree) if isinstance(node, ast.JoinedStr)
            for part in node.values
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                value = node.value
                if (isinstance(value, ast.BinOp) and isinstance(value.op, ast.Div)
                        and isinstance(value.left, ast.Name) and value.left.id in aliases
                        and isinstance(value.right, ast.Constant) and isinstance(value.right.value, str)):
                    aliases[node.targets[0].id] = aliases[value.left.id] + "/" + value.right.value
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                fragments.append("".join(
                    part.value if isinstance(part, ast.Constant) and isinstance(part.value, str)
                    else aliases.get(part.value.id, "{expression}")
                    if isinstance(part, ast.FormattedValue) and isinstance(part.value, ast.Name)
                    else "{expression}" for part in node.values
                ))
            elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
                  and id(node) not in formatted_parts):
                fragments.append(node.value)
    declared = entry.get("agent_write_paths", ())
    if isinstance(declared, list):
        fragments.extend(value for value in declared if isinstance(value, str))
    env = entry.get("env", {})
    if isinstance(env, dict):
        fragments.extend(value for key, value in env.items()
                         if isinstance(value, str) and "DRAFT" in key.upper())
    for fragment in fragments:
        if not _WRITE.search(fragment) and fragment not in declared:
            continue
        if re.search(r"\b(?:in (?:that|the|a) worktree|proposal worktree)\b", fragment, re.I):
            continue
        for match in _PATH.finditer(fragment):
            path = match.group().rstrip(".,;:)]}`")
            path = path.replace("${STATE_DIR}", f"state/pollers/{name}").replace(
                "$STATE_DIR", f"state/pollers/{name}"
            ).replace("${MIMIR_HOME}/", "").replace("$MIMIR_HOME/", "")
            path = path.replace("${POLLER_NAME}", name)
            if tainted_file_write_target(str(home / path), home=home):
                paths.add(path)
    return tuple(sorted(paths))
