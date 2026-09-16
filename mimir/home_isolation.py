"""Best-effort measurement, not an authorization or sandbox boundary."""

from __future__ import annotations

import logging
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

log = logging.getLogger(__name__)

# Only an explicit permission denial, after a successful traversal control, is
# evidence of isolation. Interpreter/identity/path failures must remain unknown.
_READ_PROBE = """
import os, pathlib, sys
owner, uid = map(int, sys.argv[1:3])
if os.getuid() != uid or os.geteuid() != uid or uid in (0, owner):
    sys.exit(2)
probe, control = map(pathlib.Path, sys.argv[3:5])
if control.read_bytes() != b'mimir-home-isolation-probe':
    sys.exit(2)
try:
    content = probe.read_bytes()
except PermissionError:
    sys.exit(10)
sys.exit(11 if content == b'mimir-home-isolation-probe' else 2)
"""


def _home_filesystem(home: Path) -> str | None:
    """Find the most specific enclosing Linux mount, without needing privilege.

    Resolve symlinks and compare path components (not string prefixes). Equal
    mountpoints can be stacked; without resolving which is visible, stay unknown.
    Unavailable or malformed mount metadata is not evidence of isolation.
    """
    target = home.resolve(strict=True)
    matches: list[tuple[int, str]] = []
    with Path("/proc/self/mountinfo").open(encoding="utf-8") as mounts:
        for line in mounts:
            before, after = line.rstrip("\n").split(" - ", 1)
            fields, filesystem = before.split(), after.split()
            if len(fields) < 6 or len(filesystem) < 3:
                raise ValueError("malformed mountinfo record")
            # mountinfo escapes whitespace and backslashes using octal digits.
            mountpoint = Path(re.sub(
                r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[4],
            ))
            if not mountpoint.is_absolute():
                raise ValueError("relative mountinfo mountpoint")
            if target.is_relative_to(mountpoint):
                matches.append((len(mountpoint.parts), filesystem[0]))
    if not matches:
        return None
    depth = max(depth for depth, _ in matches)
    closest = [kind for size, kind in matches if size == depth]
    return closest[0] if len(closest) == 1 else None


def check_home_isolation(home: Path) -> str:
    """Log measured isolation or inferred exposure without making boot depend on it.

    Never elevate privileges or change the server's identity to measure. When a
    read probe cannot decide, virtiofs metadata warrants an ERROR, explicitly an
    inference rather than proof that a non-owning uid successfully read a file.
    """
    verdict = "unknown"
    detail = "probe unavailable"
    paths: list[Path] = []
    try:
        import pwd

        owner = os.geteuid()
        if owner != 0:
            raise RuntimeError("cannot switch to a non-owning uid without privilege")
        account = next(
            (entry for entry in pwd.getpwall() if entry.pw_uid not in (0, owner)),
            None,
        )
        if account is None:
            raise RuntimeError("no non-owning non-root uid available")
        for mode in (0o600, 0o644):
            fd, name = tempfile.mkstemp(prefix=".mimir-isolation-", dir=home)
            paths.append(Path(name))
            with os.fdopen(fd, "wb") as stream:
                stream.write(b"mimir-home-isolation-probe")
                os.fchmod(stream.fileno(), mode)
                info = os.fstat(stream.fileno())
                if info.st_uid != owner or stat.S_IMODE(info.st_mode) != mode:
                    raise RuntimeError("probe ownership/mode could not be established")
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-c", _READ_PROBE,
             str(owner), str(account.pw_uid), *(str(path) for path in paths)],
            user=account.pw_uid, group=account.pw_gid, extra_groups=[],
            cwd="/", env={}, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=5, check=False,
        )
        verdict = {10: "isolated", 11: "exposed"}.get(result.returncode, "unknown")
        detail = f"non-owning uid={account.pw_uid}, probe exit={result.returncode}"
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
    finally:
        for path in paths:
            try:
                path.unlink()
            except OSError as exc:
                log.warning("Home isolation probe cleanup failed for %s: %s", path, exc)

    if verdict == "unknown":
        try:
            filesystem = _home_filesystem(home)
            detail += f"; mount filesystem={filesystem or 'undetermined'}"
            if filesystem == "virtiofs":
                verdict = "suspected-exposed"
        except Exception as exc:
            detail += f"; mount lookup unavailable: {type(exc).__name__}: {exc}"

    if verdict == "suspected-exposed":
        log.error(
            "Home uid isolation: suspected-exposed home=%s (%s). Home is on "
            "virtiofs, which was measured ignoring guest ownership on the affected "
            "macOS deployment. Mount-type inference only, NOT a successful read "
            "probe; do not trust uid isolation for credentials or private state. "
            "Verify storage isolation with a non-owning uid read.",
            home, detail,
        )
    elif verdict == "exposed":
        log.error(
            "Home uid isolation: exposed home=%s (%s). A non-owning uid read a "
            "0600 probe; credentials and private state here are NOT uid-isolated.",
            home, detail,
        )
    elif verdict == "isolated":
        log.info("Home uid isolation: isolated home=%s (%s); probe only, not a sandbox guarantee",
                 home, detail)
    else:
        log.warning("Home uid isolation: unknown home=%s (%s); do not assume uid isolation",
                    home, detail)
    return verdict
