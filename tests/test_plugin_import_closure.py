"""The disabled coding surface must not be imported by core entry points."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from mimir.http_ingress import (
    HTTP_EVENT_INGRESS_EXTRA_VALUE,
    sanitize_http_extra,
    stamp_http_event_ingress_extra,
)
from mimir.access_control import HTTP_EVENT_INGRESS_EXTRA_KEY


FORBIDDEN = (
    "mimir.worklink", "mimir.forge", "mimir.tools.forge", "mimir.tools.repo",
    "mimir.pr_checkout_lease", "mimir.project_tests", "mimir.repo_tools",
)


@pytest.mark.parametrize("root", (
    "mimir.server", "mimir.agent", "mimir.tools", "mimir.access_control",
))
def test_coding_disabled_import_closure(root: str) -> None:
    script = """
import importlib
import sys
importlib.import_module(sys.argv[1])
forbidden = sys.argv[2:]
loaded = sorted(name for name in sys.modules
                if any(name == prefix or name.startswith(prefix + '.')
                       for prefix in forbidden))
assert not loaded, loaded
"""
    env = os.environ.copy()
    env.pop("MIMIR_PLUGINS", None)
    env["MIMIR_CODING_ENABLED"] = "0"
    result = subprocess.run(
        [sys.executable, "-c", script, root, *FORBIDDEN],
        env=env, capture_output=True, text=True, check=False, timeout=60,
    )
    assert result.returncode == 0, result.stderr


def test_http_ingress_strips_nested_hints_and_stamps_server_marker() -> None:
    extra = {
        "data": [{"issue_id": 42, "safe": "retained"}],
        HTTP_EVENT_INGRESS_EXTRA_KEY: "spoofed",
    }
    sanitized = sanitize_http_extra(extra)
    assert sanitized == {"data": [{"safe": "retained"}]}
    assert stamp_http_event_ingress_extra(sanitized) == {
        "data": [{"safe": "retained"}],
        HTTP_EVENT_INGRESS_EXTRA_KEY: HTTP_EVENT_INGRESS_EXTRA_VALUE,
    }
    assert extra["data"][0]["issue_id"] == 42
