from __future__ import annotations

import sys


def main() -> int | None:
    if sys.argv[1:2] == ["acp"]:
        from mimir.acp.bootstrap import main as acp_main

        return acp_main(sys.argv[2:])

    from mimir.cli import main as cli_main

    cli_main()
    return None
