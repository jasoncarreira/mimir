from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from mimir import access_control as ac


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["home", "outside", "symlink", "missing", "loop", "invalid"])
async def test_file_search_publishes_strictly_resolved_sources(tmp_path, monkeypatch, location):
    import json
    from unittest.mock import AsyncMock

    from mimir.search import HashEmbedder, Indexer, SearchResult
    from mimir.tools import extra

    home = tmp_path / "home"
    target = home / "memory" / "note.md"
    target.parent.mkdir(parents=True)
    target.write_text("reference")
    outside = tmp_path / "external.md"
    outside.write_text("external")
    link = home / "memory" / "link.md"
    link.symlink_to(outside)
    loop = home / "memory" / "loop.md"
    loop.symlink_to(loop)
    path = {
        "home": "memory/note.md", "outside": str(outside),
        "symlink": "memory/link.md", "missing": "memory/missing.md",
        "loop": "memory/loop.md", "invalid": "memory/\0.md",
    }[location]
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.delenv("MIMIR_SOURCE_REPO", raising=False)
    monkeypatch.chdir(tmp_path)
    results = [
        SearchResult(p, "memory", 0, 1, 1, 1, 1, "snippet", None)
        for p in ("memory/note.md", path)
    ]
    indexer = Indexer(home, embedder=HashEmbedder())
    monkeypatch.setattr(indexer, "search", AsyncMock(return_value=results))
    monkeypatch.setitem(extra._SEARCH_STATE, "indexer", indexer)
    token = ac.begin_protected_result_capture()
    try:
        # An earlier publication must not make an incomplete capture authoritative.
        ac.publish_protected_result(())
        payload = await extra.file_search.coroutine(query="reference")
    finally:
        provenance = ac.end_protected_result_capture(token)

    assert json.loads(payload) == [r.to_dict() for r in results]
    if location in {"missing", "loop", "invalid"}:
        assert provenance is None
    else:
        assert provenance is not None
        source = provenance.sources[-1]
        expected_path = target if location == "home" else outside
        expected = (
            ac.FilesystemReadTrust.WRITE_SIDE_GATING if location == "home"
            else ac.FilesystemReadTrust.UNANCHORED
        )
        assert source.resource_id == str(expected_path.resolve(strict=True))
        assert ac._filesystem_read_trust_anchor(source.resource_id) is expected
        assert (source.integrity, source.integrity_effect) == expected.integrity


@pytest.mark.parametrize("relative", [".", "attachments/body", "unknown/file", "state/pollers/event"])
def test_home_helper_rejects_nonreference_paths(tmp_path: Path, relative: str) -> None:
    assert ac._home_reference_integrity(tmp_path, Path(relative)) == "untrusted"


@pytest.mark.parametrize("relative", ["skills", "skills/example/SKILL.md", "skills/example/script.py"])
def test_home_helper_trusts_skills_without_records(tmp_path: Path, relative: str) -> None:
    assert ac._home_reference_integrity(tmp_path, Path(relative)) == "trusted"


@pytest.mark.parametrize("relative, expected", [
    ("skills/example/SKILL.md", ac.FilesystemReadTrust.WRITE_SIDE_GATING),
    ("state/notes.txt", ac.FilesystemReadTrust.WRITE_SIDE_GATING),
    ("attachments/fetch-cache/body", ac.FilesystemReadTrust.HOME_CARVE_OUT),
    ("state/pollers/event", ac.FilesystemReadTrust.HOME_CARVE_OUT),
    ("other/body", ac.FilesystemReadTrust.HOME_UNANCHORED),
    (".", ac.FilesystemReadTrust.HOME_UNANCHORED),
])
@pytest.mark.parametrize("overlap", ["ancestor", "home", "subtree"])
def test_named_home_decision_wins_over_source_root(tmp_path, monkeypatch, relative, expected, overlap):
    home = tmp_path / "home"
    home.mkdir()
    target = home / relative
    if relative != ".":
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("content")
    source = {"ancestor": tmp_path, "home": home, "subtree": target.parent}[overlap]
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv("MIMIR_SOURCE_REPO", str(source))
    assert ac._filesystem_read_trust_anchor(str(target)) is expected
    assert ac._filesystem_result_integrity(None, str(target)) == expected.integrity


@pytest.mark.parametrize("configuration", ["valid", "absent", "missing", "file", "outside", "missing_home", "missing_resource", "escape"])
def test_named_root_decision_fails_closed(tmp_path, monkeypatch, configuration):
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "file"
    target.write_text("content")
    outside = tmp_path / "outside"
    outside.write_text("external")
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv("MIMIR_SOURCE_REPO", str(repo))
    if configuration == "absent":
        monkeypatch.delenv("MIMIR_SOURCE_REPO")
    elif configuration == "missing":
        monkeypatch.setenv("MIMIR_SOURCE_REPO", str(tmp_path / "missing"))
    elif configuration == "file":
        monkeypatch.setenv("MIMIR_SOURCE_REPO", str(target))
    elif configuration == "outside":
        target = outside
    elif configuration == "missing_home":
        monkeypatch.setenv("MIMIR_HOME", str(home / "missing"))
    elif configuration == "missing_resource":
        target = repo / "missing"
    elif configuration == "escape":
        target = repo / "escape"
        target.symlink_to(outside)
    expected = ac.FilesystemReadTrust.ROOT_MEMBERSHIP if configuration == "valid" else ac.FilesystemReadTrust.UNANCHORED
    assert ac._filesystem_read_trust_anchor(str(target)) is expected
    assert ac._filesystem_result_integrity(None, str(target)) == expected.integrity


@pytest.mark.parametrize("mismatch", [None, "verdict", "scope_id", "canonical_repo", "pr_number", "observed_head_sha", "no_lease"])
def test_named_author_anchor_requires_bound_attestation(tmp_path, monkeypatch, mismatch):
    from mimir import pr_checkout_lease

    home = tmp_path / "home"
    target = home / "state" / "pollers" / "event"
    target.parent.mkdir(parents=True)
    target.write_text("external")
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv("MIMIR_SOURCE_REPO", str(home))
    lease = SimpleNamespace(scope_id="scope", canonical_repo="owner/repo", pr_number=7, head_sha="abc")
    scope = SimpleNamespace(scope_id="scope", canonical_repo="owner/repo", pr_number=7, observed_head_sha="abc")
    if mismatch in {"scope_id", "canonical_repo", "pr_number", "observed_head_sha"}:
        setattr(scope, mismatch, "different")
    auth = SimpleNamespace(
        canonical_principal="reader", repo_pr_action_scope=scope,
        ifc_state=SimpleNamespace(pr_checkout_author_trust={scope.scope_id: mismatch != "verdict"}),
    )
    monkeypatch.setattr(pr_checkout_lease, "active_pr_checkout_lease_for_path", lambda _: None if mismatch == "no_lease" else lease)
    decisions = []
    classify = ac._filesystem_read_trust_anchor

    def capture(*args, **kwargs):
        decision = classify(*args, **kwargs)
        decisions.append(decision)
        return decision

    monkeypatch.setattr(ac, "_filesystem_read_trust_anchor", capture)
    source = ac.protected_result_source(
        auth, principal="filesystem", domain="filesystem",
        resource_id=str(target), bridge_instance="filesystem",
    )
    expected = ac.FilesystemReadTrust.AUTHOR_ATTESTATION if mismatch is None else ac.FilesystemReadTrust.HOME_CARVE_OUT
    assert decisions == [expected]
    assert (source.integrity, source.integrity_effect) == expected.integrity
    assert source.domain == ("repository" if mismatch is None else "filesystem")
    assert source.resource_id == ("owner/repo#pull/7@abc" if mismatch is None else str(target))


@pytest.mark.parametrize("decision", list(ac.FilesystemReadTrust))
def test_named_anchor_integrity_projection(decision):
    trusted = decision in {
        ac.FilesystemReadTrust.AUTHOR_ATTESTATION,
        ac.FilesystemReadTrust.WRITE_SIDE_GATING,
        ac.FilesystemReadTrust.ROOT_MEMBERSHIP,
    }
    assert decision.integrity == (("trusted", "informational") if trusted else ("untrusted", "active_ingest"))


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("boundary", ["ordinary", "denied", "provenance", "job_failure"])
def test_shell_result_integrity_depends_on_provenance_not_exit(failed, boundary):
    from mimir.models import SourceLabel

    tool_name = "bash_job_output" if boundary == "job_failure" else "shell_exec"
    provenance = None
    if boundary == "provenance":
        provenance = ac.ProtectedResultProvenance(sources=(SourceLabel(
            principal="external", domain="web", resource_id="https://example.test",
            bridge_instance="fetch_url", sensitivity="internal",
            integrity="untrusted", integrity_effect="active_ingest",
        ),))
    labels = ac.classify_protected_result(
        tool_name, {}, None, ac.ToolAuthorization(
            tool_name=tool_name, decision=ac.OperationDecision.ADMIN_REQUIRED,
            allowed=boundary != "denied",
        ),
        result=f"exit={1 if failed else 0}\nidentical output",
        provenance=provenance, failed=failed,
    )
    assert labels is not None
    effect = (
        "informational"
        if boundary == "ordinary" or (boundary == "job_failure" and not failed)
        else "active_ingest"
    )
    assert {(source.integrity, source.integrity_effect) for source in labels.sources} == {
        ("untrusted", effect),
    }


_RECOVERY_RESULT_DOMAINS = {
    "worklink_recovery_inspect": "worklink_recovery_state",
    "worklink_recovery_list": "worklink_recovery_checkout",
    "worklink_recovery_read": "worklink_recovery_checkout",
    "worklink_recovery_write": "worklink_recovery_control",
    "worklink_recovery_delete": "worklink_recovery_control",
    "worklink_recovery_test": "worklink_recovery_test",
    "worklink_recovery_commit": "worklink_recovery_control",
    "worklink_recovery_resume": "worklink_recovery_control",
}


def _recovery_result_source(tool_name: str):
    from mimir.models import SourceLabel

    return SourceLabel(
        principal="service:poller:worklink-ready-queue",
        domain=_RECOVERY_RESULT_DOMAINS[tool_name],
        resource_id=f"retained:{tool_name}",
        bridge_instance="worklink_recovery",
        sensitivity="internal",
        authorized_principals=frozenset({
            "service:poller:worklink-ready-queue",
        }),
        source_kind="protected_tool",
        integrity="trusted",
        integrity_effect="informational",
    )


def _recovery_authorization(tool_name: str) -> ac.ToolAuthorization:
    return ac.ToolAuthorization(
        tool_name=tool_name,
        decision=ac.OperationDecision.RESOURCE_SCOPED,
        allowed=True,
    )


@pytest.mark.parametrize("tool_name", sorted(_RECOVERY_RESULT_DOMAINS))
def test_recovery_forged_source_without_grant_fails_closed(
    tool_name: str,
) -> None:
    source = _recovery_result_source(tool_name)

    labels = ac.classify_protected_result(
        tool_name,
        {"recovery_handle": "opaque"},
        None,
        _recovery_authorization(tool_name),
        result='{"ok": true}',
        provenance=ac.ProtectedResultProvenance((source,)),
    )

    assert labels is not None
    assert {
        (item.domain, item.integrity, item.integrity_effect)
        for item in labels.sources
    } == {(_RECOVERY_RESULT_DOMAINS[tool_name], "untrusted", "active_ingest")}


@pytest.mark.parametrize("tool_name", sorted(_RECOVERY_RESULT_DOMAINS))
def test_recovery_missing_publication_fails_closed(tool_name: str) -> None:
    labels = ac.classify_protected_result(
        tool_name,
        {"recovery_handle": "opaque"},
        None,
        _recovery_authorization(tool_name),
        result='{"ok": true}',
    )

    assert labels is not None
    assert {
        (source.domain, source.integrity, source.integrity_effect)
        for source in labels.sources
    } == {(_RECOVERY_RESULT_DOMAINS[tool_name], "untrusted", "active_ingest")}
    assert ac._has_untrusted_active_ingest(None, labels) is True


def test_recovery_unbound_empty_list_publication_fails_closed() -> None:
    labels = ac.classify_protected_result(
        "worklink_recovery_list",
        {"recovery_handle": "opaque"},
        None,
        _recovery_authorization("worklink_recovery_list"),
        result='{"ok": true, "files": []}',
        provenance=ac.ProtectedResultProvenance(()),
    )

    assert labels is not None
    assert ac._has_untrusted_active_ingest(None, labels) is True


def test_recovery_unbound_handled_failure_fails_closed() -> None:
    source = _recovery_result_source("worklink_recovery_test")

    labels = ac.classify_protected_result(
        "worklink_recovery_test",
        {"recovery_handle": "opaque"},
        None,
        _recovery_authorization("worklink_recovery_test"),
        result='{"ok": false, "code": "tests_failed"}',
        provenance=ac.ProtectedResultProvenance((source,)),
        failed=True,
    )

    assert labels is not None
    assert ac._has_untrusted_active_ingest(None, labels) is True


def test_recovery_unexpected_exception_invalidates_earlier_publication() -> None:
    source = _recovery_result_source("worklink_recovery_test")

    labels = ac.classify_protected_result(
        "worklink_recovery_test",
        {"recovery_handle": "opaque"},
        None,
        _recovery_authorization("worklink_recovery_test"),
        result=RuntimeError("unexpected test runner failure"),
        provenance=ac.ProtectedResultProvenance((source,)),
        failed=True,
    )

    assert labels is not None
    assert {
        (item.domain, item.integrity, item.integrity_effect)
        for item in labels.sources
    } == {("worklink_recovery_test", "untrusted", "active_ingest")}


def test_recovery_policy_refusal_adds_no_result_source() -> None:
    from langchain_core.messages import ToolMessage

    from mimir.tools.refusals import ToolPolicyRefusal

    refusal = ToolPolicyRefusal("recovery path is outside the admitted checkout")
    result = ToolMessage(
        content=str(refusal),
        tool_call_id="recovery-refusal",
        name="worklink_recovery_read",
        status="error",
    )

    labels = ac.classify_protected_result(
        "worklink_recovery_read",
        {"recovery_handle": "opaque", "relative_path": "../escape"},
        None,
        _recovery_authorization("worklink_recovery_read"),
        result=result,
        policy_refusal=refusal,
        failed=True,
    )

    assert labels is None


def test_external_read_remains_active_ingest_beside_retained_read() -> None:
    from mimir.models import InformationFlowLabels, SourceLabel

    retained = InformationFlowLabels().with_source(
        _recovery_result_source("worklink_recovery_read")
    )
    external = InformationFlowLabels().with_source(SourceLabel(
        principal="external",
        domain="filesystem",
        resource_id="/outside/retained-checkout",
        bridge_instance="filesystem",
        sensitivity="internal",
        source_kind="protected_tool",
        integrity="untrusted",
        integrity_effect="active_ingest",
    ))

    assert ac._has_untrusted_active_ingest(None, retained) is False
    assert ac._has_untrusted_active_ingest(None, external) is True
