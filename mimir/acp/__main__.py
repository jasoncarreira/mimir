from __future__ import annotations

import sys


def main() -> int:
    from mimir.acp.bootstrap import main as bootstrap_main

    return bootstrap_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
