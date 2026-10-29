"""Shared fakes for tracker tests. Later slices extend these locally, never here."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from typing import Any


class FakeChainlink:
    """An in-memory Chainlink CLI: issue search, show, create, label and comment.

    Use an instance as the ``runner`` of ``ChainlinkWork``. ``calls`` holds every argv
    after the binary name; ``fail`` makes the named subcommand (``"search"``) exit 1.
    """

    def __init__(self, *, next_id: int = 9137) -> None:
        self.issues: dict[int, dict[str, Any]] = {}
        self.calls: list[tuple[str, ...]] = []
        self.fail: set[str] = set()
        self.silent_create = False
        self.next_id = next_id

    def add(
        self,
        description: str,
        *,
        labels: Sequence[str] = (),
        comments: Sequence[str] = (),
        status: str = "open",
        title: str = "Existing",
    ) -> int:
        issue_id = self.next_id
        self.next_id += 1
        self.issues[issue_id] = {
            "id": issue_id,
            "title": title,
            "description": description,
            "status": status,
            "labels": list(labels),
            "comments": [{"content": text} for text in comments],
        }
        return issue_id

    def count(self, verb: str) -> int:
        return sum(1 for call in self.calls if call[:2] == ("issue", verb))

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        args = tuple(argv[1:])
        self.calls.append(args)
        if args[0] != "issue" or len(args) < 2:
            return self._done(1)
        verb = args[1]
        if verb in self.fail:
            return self._done(1, err=f"{verb} failed")
        if verb == "search":
            term = args[2]
            hits = [
                {"id": i["id"], "title": i["title"], "description": i["description"]}
                for i in self.issues.values()
                if term in i["title"] or term in i["description"]
            ]
            return self._done(0, json.dumps(hits))
        if verb == "show":
            issue = self.issues.get(int(args[2]))
            if issue is None:
                return self._done(1, err="not found")
            return self._done(0, json.dumps(issue))
        if verb == "create":
            labels = [args[n + 1] for n, a in enumerate(args) if a == "--label"]
            description = args[args.index("--description") + 1]
            issue_id = self.add(description, labels=labels, title=args[2])
            return self._done(0, "" if self.silent_create else f"Created issue #{issue_id}\n")
        if verb == "label":
            labels = self.issues[int(args[2])]["labels"]
            if args[3] not in labels:
                labels.append(args[3])
            return self._done(0)
        if verb == "unlabel":
            labels = self.issues[int(args[2])]["labels"]
            if args[3] in labels:
                labels.remove(args[3])
            return self._done(0)
        if verb == "comment":
            self.issues[int(args[2])]["comments"].append({"content": args[3]})
            return self._done(0)
        return self._done(1)

    @staticmethod
    def _done(code: int, out: str = "", err: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], code, out, err)
