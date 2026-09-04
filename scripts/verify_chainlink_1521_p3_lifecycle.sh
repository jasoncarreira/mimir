#!/bin/sh
set -eu

uv run pytest -q tests/test_acp_proxy.py
uv run pytest -q tests/test_acp_ssh.py
uv run pytest -q tests/test_acp_hosted.py
uv run pytest -q tests/test_acp_python_kernel.py
uv run pytest -q tests/test_acp_sessions.py
uv run pytest -q tests/test_acp_shutdown.py
uv run pytest -q tests/test_acp_packaging.py
uv run pytest -q tests/test_acp_dependency_closure.py
uv run pytest -q tests/test_acp_registry.py
uv run pytest -q tests/test_bench_via_mimir.py
uv run pytest -q
env MIMIR_ACCESS_CONTROL_ENFORCED=1 uv run pytest -q --tb=short
