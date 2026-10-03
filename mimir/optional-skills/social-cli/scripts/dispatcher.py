"""Dispatch only forge-merged, HEAD-clean social outboxes from the poller process."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
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


def _withheld(poller: str, reason: str, path: str | None = None,
              stderr: str | None = None) -> None:
    """Signal the parent event logger without enqueueing an agent turn."""
    event = {"poller": poller, "signal": "social_outbox_dispatch_withheld",
             "reason": reason, "path": path}
    if stderr is not None:
        event["stderr"] = stderr[:500]
    print(json.dumps(event), flush=True)


SCRIPTS = Path(__file__).resolve().parent
CAP = 5
OUTBOX_NAME = re.compile(r"outbox-.+\.yaml\Z")


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
    from mimir.social_outbox import load_outbox, validate_outbox

    doc = load_outbox(text)
    if validate_outbox(doc):
        return None
    proposed: dict[str, int] = {}
    for entry in doc["dispatch"]:
        action, payload = next(iter(entry.items()))
        if action not in {"post", "reply", "thread"}:
            continue
        platforms = payload.get("platforms", [payload.get("platform")])
        if isinstance(platforms, dict):
            platforms = platforms.keys()
        units = len(payload["posts"]) if action == "thread" else 1
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


def _flag_unrecognized(home: Path, root: Path, state_dir: Path, poller: str) -> None:
    """Flag inert regular files once per path and content, across poller fires."""
    ledger = state_dir / "unrecognized-outbox-ledger.jsonl"
    try:
        with ledger.open("a+", encoding="utf-8") as log:
            fcntl.flock(log, fcntl.LOCK_EX)
            log.seek(0)
            seen = {(entry["path"], entry["sha256"])
                    for line in log if line.strip() for entry in (json.loads(line),)}
            for path in sorted(root.iterdir()):
                if path.is_symlink() or not path.is_file() or OUTBOX_NAME.fullmatch(path.name):
                    continue
                rel = path.relative_to(home).as_posix()
                try:
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                except OSError:
                    _withheld(poller, "outbox_read_failed", rel)
                    continue
                if (rel, digest) in seen:
                    continue
                log.seek(0, os.SEEK_END)
                log.write(json.dumps({"path": rel, "sha256": digest}) + "\n")
                log.flush()
                os.fsync(log.fileno())
                seen.add((rel, digest))
                _withheld(poller, "unrecognized_outbox_name", rel)
    except (OSError, ValueError, KeyError, TypeError):
        _withheld(poller, "invalid_unrecognized_outbox_ledger")


def dispatch_merged(home: Path, state_dir: Path, poller: str, bin_path: str) -> None:
    """Consume each merged file at most once, including on dispatch failure."""
    root = home / "state" / "social-outbox" / poller
    if not root.is_dir() or root.resolve() != root:
        return
    try:
        _ensure_mimir_import_path()
        import yaml
        from mimir.outbound_privacy import findings_require_refusal, scan_outbound
        from mimir.proposals import merged_social_outbox_commit
        from mimir.social_outbox import load_outbox, outbox_platform
    except ImportError:
        _withheld(poller, "mimir_import_failure")
        print("social-cli: mimir import failed; withholding dispatch only", file=sys.stderr)
        return
    ledger = state_dir / "dispatched-ledger.jsonl"
    state_dir.mkdir(parents=True, exist_ok=True)
    _flag_unrecognized(home, root, state_dir, poller)
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
            _withheld(poller, "invalid_dispatch_ledger")
            print("social-cli: invalid dispatched ledger; refusing dispatch", file=sys.stderr)
            return
        for path in sorted(root.glob("outbox-*.yaml")):
            if not OUTBOX_NAME.fullmatch(path.name):
                continue
            if not path.is_file() or path.is_symlink():
                _withheld(poller, "nonregular_or_symlink_outbox", path.relative_to(home).as_posix())
                continue
            rel = path.relative_to(home).as_posix()
            try:
                tracked = _run(["git", "ls-files", "--stage", "--", rel], home)
                clean = _run(["git", "diff", "HEAD", "--quiet", "--", rel], home)
                staged = _run(["git", "diff", "--cached", "HEAD", "--quiet", "--", rel], home)
                text = path.read_bytes().decode("utf-8")
            except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
                _withheld(poller, "outbox_read_failed", rel)
                print(f"social-cli: outbox check failed for {rel}: {exc}", file=sys.stderr)
                continue
            if (tracked.returncode != 0 or not tracked.stdout.startswith("100644 ")
                    or clean.returncode != 0 or staged.returncode != 0):
                _withheld(poller, "untracked_or_content_changed", rel)
                print(f"social-cli: skipping unmerged or dirty outbox {rel}", file=sys.stderr)
                continue
            if merged_social_outbox_commit(
                home, poller, rel, verified_text=text,
                on_withheld=lambda reason: _withheld(poller, reason, rel),
            ) is None:
                print(f"social-cli: forge merge approval unavailable for {rel}", file=sys.stderr)
                continue
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if digest in seen:
                continue
            try:
                if findings_require_refusal(scan_outbound((text,), tool="dispatch", sink_category="network")):
                    _withheld(poller, "privacy_scan_refused", rel)
                    print(f"social-cli: privacy scan refused {rel}", file=sys.stderr)
                    continue
                posts = _cap_allows(text, state_dir, reserved)
                if posts is None:
                    _withheld(poller, "cap_check_refused", rel)
                    print(f"social-cli: cap check refused {rel}", file=sys.stderr)
                    continue
                platform = outbox_platform(load_outbox(text))
                if platform is None:
                    # Ignore-only files retain the configured legacy inbox default.
                    platform = os.environ.get("MIMIR_SOCIAL_PLATFORMS", "bsky").split(",")[0].strip() or "bsky"
                    if platform not in {"bsky", "x"}:
                        raise ValueError("unknown default platform for ignore-only outbox")
            except (OSError, ValueError, yaml.YAMLError, subprocess.TimeoutExpired) as exc:
                _withheld(poller, "scan_or_cap_failed", rel)
                print(f"social-cli: scan or cap failed for {rel}: {exc}", file=sys.stderr)
                continue
            try:
                # The verified snapshot, not a second read of the live path.
                # Private directory (0700) and file (0600); retain through child exit.
                with tempfile.TemporaryDirectory(prefix="social-dispatch-") as private:
                    snapshot = Path(private) / f"outbox-{platform}.yaml"
                    with snapshot.open("x", encoding="utf-8", newline="") as output:
                        snapshot.chmod(0o600)
                        output.write(text)
                    try:
                        dry = _run([bin_path, "dispatch", "--dry-run", str(snapshot)], state_dir)
                    except (OSError, subprocess.TimeoutExpired) as exc:
                        _withheld(poller, "dry_run_failed", rel, str(exc))
                        continue
                    if dry.returncode != 0 or re.search(
                        r"(?:validation\s+(?:failed|failure|error)|"
                        r"action\s+\d+:.*(?:error|invalid|failed|no recognized))",
                        dry.stdout + "\n" + dry.stderr, re.IGNORECASE,
                    ):
                        _withheld(poller, "dry_run_failed", rel, dry.stderr)
                        continue
                    log.seek(0, os.SEEK_END)
                    log.write(json.dumps({"sha256": digest, "path": rel, "day": today, "posts": posts}) + "\n")
                    log.flush()
                    os.fsync(log.fileno())
                    seen.add(digest)
                    for reserved_platform, units in posts.items():
                        reserved[reserved_platform] = reserved.get(reserved_platform, 0) + units
                    result = _run([bin_path, "dispatch", str(snapshot)], state_dir)
                    if result.returncode != 0:
                        _withheld(poller, "dispatch_failed", rel, result.stderr)
                        print(f"social-cli: dispatch failed for {rel}: {result.stderr[:200]}", file=sys.stderr)
            except (OSError, subprocess.TimeoutExpired) as exc:
                _withheld(poller, "dispatch_failed", rel, str(exc))
                print(f"social-cli: dispatch failed for {rel}: {exc}", file=sys.stderr)
