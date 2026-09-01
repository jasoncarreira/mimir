from __future__ import annotations

import os
import sys


PREFIX = b"acp-test-banner-before-exec\n"


def _write_all(fd: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        try:
            written = os.write(fd, remaining)
        except InterruptedError:
            continue
        if written <= 0:
            raise OSError("unable to write banner prefix")
        remaining = remaining[written:]


def main() -> None:
    _write_all(1, PREFIX)
    os.execv(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
