from __future__ import annotations

import os
import subprocess
import sys


TEST_MODULES = (
    "tests/test_worklink_attention.py",
    "tests/test_worklink_dispatch_failures.py",
    "tests/test_worklink_claims.py",
    "tests/test_worklink_orchestrator.py",
    "tests/test_worklink_autonomy.py",
    "tests/test_worklink_cli.py",
    "tests/test_worklink_backends.py",
    "tests/test_worklink_evidence.py",
    "tests/test_worklink_reattach.py",
    "tests/test_server.py",
    "tests/test_pollers.py",
    "tests/test_poller_recovery.py",
    "tests/test_agent.py",
    "tests/test_tool_registry.py",
    "tests/test_access_control.py",
    "tests/test_operator_alert.py",
    "tests/test_optional_skill_poller_entrypoints.py",
    "tests/test_wheel_package_data.py",
    "tests/test_worklink_factory_docs.py",
    "tests/test_worklink_pipeline_docs.py",
)


def run(argv: list[str], env: dict[str, str] | None = None) -> int:
    return subprocess.run(argv, env=env, shell=False, check=False).returncode


def main() -> int:
    failed = False
    for module in TEST_MODULES:
        failed = run([sys.executable, "-m", "pytest", "-q", "-n", "0", module]) != 0 or failed
    failed = run([sys.executable, "-m", "pytest", "-q"]) != 0 or failed
    unenforced = dict(os.environ)
    unenforced.pop("MIMIR_ACCESS_CONTROL_ENFORCED", None)
    failed = run(
        [sys.executable, "-m", "pytest", "-q", "-n", "6"], env=unenforced
    ) != 0 or failed
    enforced = dict(os.environ)
    enforced["MIMIR_ACCESS_CONTROL_ENFORCED"] = "1"
    failed = run(
        [sys.executable, "-m", "pytest", "-q", "-n", "6"], env=enforced
    ) != 0 or failed
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
