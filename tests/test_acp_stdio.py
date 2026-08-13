from __future__ import annotations

import os
import sys

from mimir.acp import bootstrap
from mimir.acp.bootstrap import _reserve_stdout


def test_stdout_reservation_routes_python_output_to_stderr(capfd: object) -> None:
    saved = os.dup(1)
    previous = sys.stdout
    frame = _reserve_stdout()
    try:
        print("diagnostic", flush=True)
        os.write(frame.fileno(), b"frame\n")
    finally:
        frame.close()
        os.dup2(saved, 1)
        os.close(saved)
        sys.stdout = previous
    out, err = capfd.readouterr()
    assert out == "frame\n"
    assert err == "diagnostic\n"


def test_direct_fd_and_import_banner_cannot_contaminate_protocol_stdout(monkeypatch: object, capfd: object) -> None:
    protocol = b'{"jsonrpc":"2.0","id":1,"result":{}}\n'

    def dispatch(args: object, output: object) -> int:
        print("import-banner", flush=True)
        os.write(1, b"direct-fd-diagnostic\n")
        output.write(protocol)
        return 0

    monkeypatch.setattr(bootstrap, "_dispatch", dispatch)
    assert bootstrap.main([]) == 0
    out, err = capfd.readouterr()
    assert out.encode() == protocol
    assert err == "import-banner\ndirect-fd-diagnostic\n"
