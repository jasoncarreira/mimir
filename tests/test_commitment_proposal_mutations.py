"""Discriminating guard-removal checks in disposable subprocess packages."""
from pathlib import Path
import os
import shutil
import subprocess

import pytest
from tests.nested_pytest import pytest_command

ROOT = Path(__file__).resolve().parents[1]

MUTANTS = [
    ("authenticated_reply", "mimir/commitment_proposals.py", "or not _is_authenticated_operator(event, resolver)", "or False", "test_forged_or_wrong_channel_approval_applies_nothing[override0]"),
    ("channel", "mimir/commitment_proposals.py", "event.channel_id != backend[0]", "False", "test_forged_or_wrong_channel_approval_applies_nothing[override5]"),
    ("bulk_dismiss", "mimir/commitment_proposals.py", 'bulk and data["action"] == "dismiss"', "False", "test_bulk_completes_only_explicit_id_required_for_dismissal"),
    ("expiry", "mimir/commitment_proposals.py", "status='pending' AND expires<=?", "status='pending' AND expires<0 AND expires<=?", "test_expiry_prevents_apply"),
    ("care_proposal", "mimir/commitment_proposals.py", 'record.is_terminal() or record.sensitivity == "care"', "record.is_terminal()", "test_care_cannot_be_proposed"),
    ("care_apply", "mimir/commitments/store.py", 'record is None or record.is_terminal() or record.sensitivity == "care"', "record is None or record.is_terminal()", "test_store_guards_are_not_masked_by_fingerprint[care]"),
    ("terminal_apply", "mimir/commitments/store.py", 'record is None or record.is_terminal() or record.sensitivity == "care"', 'record is None or record.sensitivity == "care"', "test_store_guards_are_not_masked_by_fingerprint[terminal]"),
    ("owner_apply", "mimir/commitments/store.py", "or record.owner_principal != expected_owner", "or False", "test_store_guards_are_not_masked_by_fingerprint[owner]"),
    ("snapshot_apply", "mimir/commitments/store.py", "or commitment_fingerprint(record) != expected_fingerprint", "or False", "test_changed_or_terminal_commitment_skipped_and_reported[text]"),
    ("direct_taint", "mimir/tools/registry.py", '_commitment_taint_refusal(runtime, "commitment_complete")', "None", "test_tainted_scheduled_review_to_authenticated_operator_reply"),
]

@pytest.mark.parametrize("name,path,old,new,test", MUTANTS, ids=[row[0] for row in MUTANTS])
def test_guard_removal_is_detected(tmp_path, name, path, old, new, test):
    package = tmp_path / "project"
    shutil.copytree(ROOT / "mimir", package / "mimir", ignore=shutil.ignore_patterns("__pycache__"))
    (package / "tests").mkdir()
    (package / "tests/__init__.py").write_text("")
    shutil.copyfile(ROOT / "tests/test_commitment_proposals.py", package / "tests/test_commitment_proposals.py")
    source = package / path
    text = source.read_text()
    assert text.count(old) == 1, name
    source.write_text(text.replace(old, new))
    child_env = dict(os.environ, PYTHONPATH=str(package), PYTEST_ADDOPTS="")
    command = pytest_command(tmp_path, "-q", "-o", "addopts=", "-o", "asyncio_mode=auto", "tests/test_commitment_proposals.py::" + test)
    result = subprocess.run(command, cwd=package, env=child_env, capture_output=True, text=True, timeout=90)
    assert result.returncode == 1 and "FAILED tests/test_commitment_proposals.py::" + test in result.stdout, result.stdout + result.stderr
    assert "1 failed" in result.stdout and "ERROR" not in result.stdout, result.stdout
