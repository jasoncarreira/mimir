"""Best-effort measurement, not an authorization or sandbox boundary."""

from __future__ import annotations

import logging
import os
from pathlib import Path
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


def check_home_isolation(home: Path) -> str:
    """Log and return exposed/isolated/unknown without making boot depend on it.

    An unprivileged process without a way to switch UID reports unknown. Never
    elevate privileges or change the server's identity to obtain a measurement.
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

    if verdict == "exposed":
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
