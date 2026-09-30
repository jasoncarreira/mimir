"""Dispatch only forge-merged, HEAD-clean social outboxes from the poller process."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

def _ensure_mimir_import_path() -> None:
    """Bootstrap installed copies before importing the package or its deps."""
    exe = Path(sys.executable)
    venv_root = exe.parent.parent  # Do not resolve the venv's interpreter symlink.
    candidates = [Path(__file__).resolve().parents[4]]
    if source_dir := os.environ.get("MIMIR_SOURCE_DIR"):
        candidates.append(Path(source_dir))
    if venv_root.name in {".venv", "venv"}:
        candidates.append(venv_root.parent)
    for candidate in candidates:
        if (candidate / "mimir" / "__init__.py").is_file():
            source = str(candidate)
            while source in sys.path:
                sys.path.remove(source)
            sys.path.insert(0, source)
            for site in sorted((candidate / ".venv" / "lib").glob("python*/site-packages")):
                if str(site) not in sys.path:
                    sys.path.append(str(site))
            return


_ensure_mimir_import_path()

import yaml

from mimir.outbound_privacy import findings_require_refusal, scan_outbound
from mimir.proposals import merged_social_outbox_commit

SCRIPTS = Path(__file__).resolve().parent
CAP = 5


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=45, check=False)


def _count(platform: str, state_dir: Path) -> int | None:
    try:
        result = _run([sys.executable, str(SCRIPTS / "count.py"), "--platform", platform,
                       "--action", "post", "--since", "today"], state_dir)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def _cap_allows(text: str, state_dir: Path, reserved: dict[str, int]) -> dict[str, int] | None:
    doc = yaml.safe_load(text)
    if not isinstance(doc, dict) or not isinstance(doc.get("dispatch"), list):
        return None
    proposed: dict[str, int] = {}
    for entry in doc["dispatch"]:
        if not isinstance(entry, dict) or entry.get("action") not in {"post", "reply", "like", "repost", "thread"}:
            return None
        action = entry["action"]
        if action not in {"post", "reply", "thread"}:
            continue
        platforms = entry.get("platforms", [entry.get("platform", "bsky")])
        if not isinstance(platforms, list) or not platforms or any(p not in {"bsky", "x"} for p in platforms):
            return None
        units = len(entry.get("posts", [])) if action == "thread" else 1
        if not units:
            return None
        for platform in platforms:
            proposed[platform] = proposed.get(platform, 0) + units

    result = _run([sys.executable, str(SCRIPTS / "cap_check.py")], state_dir)
    if result.returncode != 0:
        return None
    match = re.search(r"\beffective=(\d+) / 5\b", result.stdout)
    if match is None:
        return None
    for platform, units in proposed.items():
        current = int(match.group(1)) if platform == "bsky" else _count(platform, state_dir)
        if current is None or max(current, reserved.get(platform, 0)) + units > CAP:
            return None
    return proposed


def dispatch_merged(home: Path, state_dir: Path, poller: str, bin_path: str) -> None:
    """Consume each merged file at most once, including on dispatch failure."""
    root = home / "state" / "social-outbox" / poller
    if not root.is_dir() or root.resolve() != root:
        return
    ledger = state_dir / "dispatched-ledger.jsonl"
    state_dir.mkdir(parents=True, exist_ok=True)
    with ledger.open("a+", encoding="utf-8") as log:
        fcntl.flock(log, fcntl.LOCK_EX)
        log.seek(0)
        try:
            entries = [json.loads(line) for line in log if line.strip()]
            seen = {entry["sha256"] for entry in entries}
            if any(not isinstance(digest, str) or len(digest) != 64 for digest in seen):
                raise ValueError("invalid sha256")
            today = datetime.now(timezone.utc).date().isoformat()
            reserved: dict[str, int] = {}
            for entry in entries:
                if entry.get("day") == today:
                    posts = entry.get("posts", {})
                    if not isinstance(posts, dict):
                        raise ValueError("invalid post reservation")
                    for platform, units in posts.items():
                        if platform not in {"bsky", "x"} or type(units) is not int or units < 0:
                            raise ValueError("invalid post reservation")
                        reserved[platform] = reserved.get(platform, 0) + units
        except (ValueError, KeyError, TypeError):
            print("social-cli: invalid dispatched ledger; refusing dispatch", file=sys.stderr)
            return
        for path in sorted(root.glob("outbox-*.yaml")):
            if not path.is_file() or path.is_symlink():
                continue
            rel = path.relative_to(home).as_posix()
            try:
                tracked = _run(["git", "ls-files", "--stage", "--", rel], home)
                clean = _run(["git", "diff", "HEAD", "--quiet", "--", rel], home)
                staged = _run(["git", "diff", "--cached", "HEAD", "--quiet", "--", rel], home)
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
                print(f"social-cli: outbox check failed for {rel}: {exc}", file=sys.stderr)
                continue
            if (tracked.returncode != 0 or not tracked.stdout.startswith("100644 ")
                    or clean.returncode != 0 or staged.returncode != 0):
                print(f"social-cli: skipping unmerged or dirty outbox {rel}", file=sys.stderr)
                continue
            if merged_social_outbox_commit(home, poller, rel) is None:
                print(f"social-cli: forge merge approval unavailable for {rel}", file=sys.stderr)
                continue
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if digest in seen:
                continue
            try:
                if findings_require_refusal(scan_outbound((text,), tool="dispatch", sink_category="network")):
                    print(f"social-cli: privacy scan refused {rel}", file=sys.stderr)
                    continue
                posts = _cap_allows(text, state_dir, reserved)
                if posts is None:
                    print(f"social-cli: cap check refused {rel}", file=sys.stderr)
                    continue
            except (OSError, ValueError, yaml.YAMLError, subprocess.TimeoutExpired) as exc:
                print(f"social-cli: scan or cap failed for {rel}: {exc}", file=sys.stderr)
                continue
            log.seek(0, os.SEEK_END)
            log.write(json.dumps({"sha256": digest, "path": rel, "day": today, "posts": posts}) + "\n")
            log.flush()
            os.fsync(log.fileno())
            seen.add(digest)
            for platform, units in posts.items():
                reserved[platform] = reserved.get(platform, 0) + units
            try:
                result = _run([bin_path, "dispatch", "--file", str(path)], state_dir)
                if result.returncode != 0:
                    print(f"social-cli: dispatch failed for {rel}: {result.stderr[:200]}", file=sys.stderr)
            except (OSError, subprocess.TimeoutExpired) as exc:
                print(f"social-cli: dispatch failed for {rel}: {exc}", file=sys.stderr)
