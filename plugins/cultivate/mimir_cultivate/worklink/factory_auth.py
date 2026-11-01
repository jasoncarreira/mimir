"""Read the attempt's worker-owned OAuth file as the factory checkout owner."""

from __future__ import annotations

import os
import stat
import sys


MAX_AUTH_BYTES = 1024 * 1024


def read_factory_auth() -> bytes:
    """Follow only the fixed sibling runtime path, anchored at the issued cwd."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    opened = [os.open(".", flags)]
    try:
        for component in ("..", ".factory-runtime", "data", "opencode"):
            opened.append(os.open(component, flags, dir_fd=opened[-1]))
        fd = os.open("auth.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                     dir_fd=opened[-1])
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600):
                raise ValueError("invalid factory auth file")
            document = os.read(fd, MAX_AUTH_BYTES + 1)
            if len(document) > MAX_AUTH_BYTES:
                raise ValueError("oversize factory auth file")
            return document
        finally:
            os.close(fd)
    finally:
        for fd in reversed(opened):
            os.close(fd)


if __name__ == "__main__":
    # Executed via the attempt-bound factory control launch, never from the checkout.
    try:
        if len(sys.argv) != 1:
            raise ValueError("invalid factory auth request")
        sys.stdout.buffer.write(read_factory_auth())
    except (OSError, ValueError):
        # Neither the path nor credential material may enter the control stderr.
        raise SystemExit(1) from None
