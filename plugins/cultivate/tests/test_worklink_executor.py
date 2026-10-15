"""Provider executor boundary and worker wire-format compatibility."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from mimir_cultivate.worklink import compute, control, dispatch_failures, worker_client, worker_exec
from mimir_cultivate.worklink.backends import opencode


def test_worker_protocol_matches_controller_and_legacy_image() -> None:
    from mimir.worklink import worker_client as legacy_client

    assert worker_client.EXECUTOR_PROTOCOL_IDENTITY == worker_exec.EXECUTOR_PROTOCOL_IDENTITY
    assert worker_client.EXECUTOR_PROTOCOL_IDENTITY == legacy_client.EXECUTOR_PROTOCOL_IDENTITY
    assert worker_client.MAX_REQUEST_BYTES == legacy_client.MAX_REQUEST_BYTES
    assert worker_client.MAX_PROJECTION_BYTES == legacy_client.MAX_PROJECTION_BYTES
    assert worker_client.DEFAULT_EXECUTOR_SOCKET == legacy_client.DEFAULT_EXECUTOR_SOCKET


def test_factory_checkout_binding_rejects_ambiguous_paths() -> None:
    root = worker_client.WORKLINK_CHECKOUT_ROOT
    checkout = root / "repo" / "1949-2" / "checkout"
    assert worker_client.factory_checkout_for_path(checkout / ".factory" / "run") == (
        checkout, 1949, 2,
    )
    for path in (
        root / "repo" / "01949-2" / "checkout",
        root / "repo" / "1949-0" / "checkout",
        checkout / ".." / "outside",
    ):
        with pytest.raises(ValueError, match="invalid factory checkout path"):
            worker_client.factory_checkout_for_path(path)
    assert worker_client.factory_checkout_for_path(root / "repo" / "1949-2" / "other") is None


def test_provider_execution_modules_do_not_import_legacy_worklink() -> None:
    modules = (compute, control, dispatch_failures, worker_client, worker_exec, opencode)
    for module in modules:
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        imports = (
            node.module for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.level == 0
        )
        assert not any(name and name.startswith("mimir.worklink") for name in imports)
