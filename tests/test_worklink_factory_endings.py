"""Closed inventory of factory-handle cancellations and attempt-tree deletion sites."""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1] / "mimir" / "worklink"

# Each entry is an ending or a guard, with its reason. Reconcile this inventory
# before adding any cancellation/deletion call in the Worklink package.
SITES = {
    ("control.py", "stop_worklink", "WorkerClient(None).cancel_factory_handle"): (1, "explicit operator stop; verified process identity"),
    ("control.py", "stop_worklink", "LocalSubprocessComputeBackend().cancel"): (2, "explicit operator stop for factory or leaf; verified process identity"),
    ("orchestrator.py", "invoke_backend", "compute.cancel"): (1, "leaf launch-state failure, not a factory handle"),
    ("evidence.py", "_run_compute_gate", "compute.cancel"): (2, "leaf evidence gate, not a factory handle"),
    ("orchestrator.py", "reattach", "LocalSubprocessComputeBackend().cancel"): (1, "explicit leaf reattach stop"),
    ("orchestrator.py", "_run_factory_070_locked", "_cancel_and_cleanup_factory_handle"): (1, "factory record save failed immediately after launch; containment refused"),
    ("orchestrator.py", "_recover_factory_070", "_cancel_and_cleanup_factory_handle"): (2, "factory terminal declared or resumed launch record save refused"),
    ("orchestrator.py", "_supervise_factory_070", "_cancel_and_cleanup_factory_handle"): (1, "wait task cannot be created; containment refused"),
    ("orchestrator.py", "_supervise_factory_070", "cancel_once"): (2, "factory terminal/park or verified death/safety refusal"),
    ("orchestrator.py", "park_for_reason", "cancel_once"): (1, "explicit budget or idle park"),
    ("orchestrator.py", "cancel_once", "compute.cancel"): (1, "shared cancellation for explicit endings or proven safety refusal"),
    ("orchestrator.py", "_cancel_and_cleanup_factory_handle", "compute.cancel"): (1, "immediate launch refusal or declared terminal recovery"),
    ("autonomy.py", "prune_stale_attempt_checkouts_for_home", "prune_attempt_checkouts"): (1, "checkout interlock and active/still-going/15-minute stop guards"),
    ("checkout.py", "prune_attempt_checkouts", "shutil.rmtree"): (2, "is_active callback from guarded prune pass; legacy direct caller supplies policy"),
    ("checkout.py", "cleanup_checkout", "rmtree_missing_ok"): (2, "completed checkout transition only"),
    ("orchestrator.py", "_cleanup_checkout_after_transition", "cleanup_checkout"): (1, "completed leaf transition only"),
    ("orchestrator.py", "_close_attempt_capabilities", "rmtree_missing_ok"): (1, "completed leaf publication, delete_checkout explicitly requested"),
    ("checkout.py", "_clone_attempt_checkout", "shutil.rmtree"): (1, "failed checkout construction before launch"),
    ("checkout.py", "create_isolated_checkout", "shutil.rmtree"): (3, "failed checkout construction before launch"),
    ("orchestrator.py", "_remove_executor_report_dir_best_effort", "rmtree_missing_ok"): (1, "temporary test report, not attempt checkout"),
    ("orchestrator.py", "_remove_executor_basetemp_best_effort", "rmtree_missing_ok"): (1, "derived pytest temp tree under trusted /tmp, only after leaf directory check; not attempt checkout"),
    ("orchestrator.py", "_make_executor_report_dir", "rmtree_missing_ok"): (1, "temporary test report, not attempt checkout"),
    ("orchestrator.py", "_create_observation_worktree", "shutil.rmtree"): (1, "temporary observation worktree, not attempt checkout"),
    ("orchestrator.py", "_remove_observation_worktree", "shutil.rmtree"): (1, "temporary observation worktree, not attempt checkout"),
}


def test_factory_cancellation_and_attempt_deletion_sites_are_closed() -> None:
    calls: Counter[tuple[str, str, str]] = Counter()
    targets = {
        "cancel_once", "compute.cancel", "_cancel_and_cleanup_factory_handle",
        "WorkerClient(None).cancel_factory_handle", "LocalSubprocessComputeBackend().cancel",
        "prune_attempt_checkouts", "cleanup_checkout", "shutil.rmtree", "rmtree_missing_ok",
    }
    for path in ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # Visit this function's own body only, not nested function calls.
            for child in ast.walk(node):
                if isinstance(child, ast.Call) and ast.unparse(child.func) in targets:
                    if any(isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef))
                           and parent is not node and parent.lineno <= child.lineno <= parent.end_lineno
                           for parent in ast.walk(node)):
                        continue
                    key = (str(path.relative_to(ROOT)), node.name, ast.unparse(child.func))
                    if ast.unparse(child.func) in {
                        "cancel_once", "compute.cancel", "_cancel_and_cleanup_factory_handle",
                        "WorkerClient(None).cancel_factory_handle", "LocalSubprocessComputeBackend().cancel",
                        "prune_attempt_checkouts", "cleanup_checkout",
                    } or (key[0] in {"checkout.py", "orchestrator.py"} and key[2] in {"shutil.rmtree", "rmtree_missing_ok"}):
                        calls[key] += 1
    expected = Counter({key: count for key, (count, reason) in SITES.items() if reason})
    assert sorted((calls - expected).items()) == []
    assert sorted((expected - calls).items()) == []
