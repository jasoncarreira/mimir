"""Tests for the change-proposal agent tools (chainlink #339/#344).

The git mechanics are covered by test_proposals.py; here we test the tool
layer — MIMIR_HOME resolution, arg forwarding, and the operator-facing message
for each outcome — with the library stubbed.

The tool functions (``open_proposal`` / ``submit_proposal`` / ``abandon_proposal``)
share names with the library functions, so the module imports the library under
private aliases (``_open_proposal`` etc.); the stubs target those.
"""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import replace
from types import SimpleNamespace

import pytest
from langchain.tools import ToolRuntime

from mimir import _context
from mimir.access_control import CapabilityTier, build_trigger_service_principal, create_auth_context
from mimir.models import AgentEvent, TurnContext, InformationFlowLabels, SourceLabel
from mimir.proposals import OpenResult, ProposalResult, PollerProposalScope, poller_worktree_path, poller_branch_name
from mimir.tools import proposals as tp
from mimir.tools.refusals import ToolPolicyRefusal


def _inv(tool, **kwargs) -> str:
    return asyncio.run(tool.ainvoke(kwargs))


# ─── open ────────────────────────────────────────────────────────────


def test_open_tool_returns_edit_path(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    wt = (tmp_path / "scratch" / "proposals" / "proposal_x").resolve()
    monkeypatch.setattr(
        tp, "_open_proposal",
        lambda home, lane="agent": OpenResult(ok=True, branch="proposal/x", worktree=wt),
    )
    out = _inv(tp.open_proposal)
    assert "scratch/proposals/proposal_x/memory/core/" in out
    assert "scratch/proposals/proposal_x/prompts/" in out
    assert "submit_proposal" in out
    assert "lane='agent'" in out


def test_open_tool_no_remote(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setattr(
        tp, "_open_proposal",
        lambda home, lane="agent": OpenResult(
            ok=False, branch=None, worktree=None, reason="no_remote", detail="x"
        ),
    )
    assert "no git remote" in _inv(tp.open_proposal).lower()


def test_open_tool_already_open(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    wt = (tmp_path / "scratch" / "proposals" / "proposal_y").resolve()
    monkeypatch.setattr(
        tp, "_open_proposal",
        lambda home, lane="agent": OpenResult(
            ok=False, branch="proposal/y", worktree=wt, reason="exists", detail="x"
        ),
    )
    out = _inv(tp.open_proposal)
    assert "already open" in out and "proposal/y" in out


def test_open_tool_forwards_lane(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    captured: dict = {}
    wt = (tmp_path / "scratch" / "proposals" / "upgrade" / "upgrade_x").resolve()

    def fake(home, lane="agent"):
        captured["lane"] = lane
        return OpenResult(ok=True, branch="upgrade/x", worktree=wt)

    monkeypatch.setattr(tp, "_open_proposal", fake)
    out = _inv(tp.open_proposal, lane="upgrade")
    assert captured == {"lane": "upgrade"}
    assert "upgrade" in out and "scratch/proposals/upgrade/upgrade_x" in out


def test_open_tool_missing_home(monkeypatch) -> None:
    monkeypatch.delenv("MIMIR_HOME", raising=False)
    assert "MIMIR_HOME not set" in _inv(tp.open_proposal)


@pytest.mark.parametrize(
    ("tool", "kwargs"),
    [
        (tp.open_proposal, {}),
        (tp.submit_proposal, {"title": "title", "rationale": "reason"}),
        (tp.abandon_proposal, {}),
    ],
)
def test_invalid_lane_returns_tool_failure_string(monkeypatch, tmp_path, tool, kwargs) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))

    result = _inv(tool, lane="manual", **kwargs)

    assert result.startswith(f"{tool.name} failed (")
    assert "unsupported proposal lane" in result


# ─── submit ──────────────────────────────────────────────────────────


def test_submit_tool_returns_url_forwards_args_and_emits_event(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    captured: dict = {}
    events: list = []

    async def fake_log(kind, **kw):
        events.append((kind, kw))

    monkeypatch.setattr(tp, "log_event", fake_log)

    def fake(home, *, title, rationale, lane="agent"):
        captured.update(title=title, rationale=rationale, lane=lane)
        return ProposalResult(
            ok=True, branch="b", pushed=True,
            pr_url="https://github.com/x/y/pull/3", reason=None,
        )

    monkeypatch.setattr(tp, "_finalize_proposal", fake)
    out = _inv(tp.submit_proposal, title="T", rationale="R", lane="upgrade")
    assert "https://github.com/x/y/pull/3" in out and "merge" in out.lower()
    assert captured == {"title": "T", "rationale": "R", "lane": "upgrade"}
    # Positive feedback event emitted with the PR URL (chainlink #337/#339/#344).
    assert events and events[0][0] == "proposal_pr_opened"
    assert events[0][1]["pr_url"] == "https://github.com/x/y/pull/3"
    assert events[0][1]["lane"] == "upgrade"


def test_submit_tool_no_open(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setattr(
        tp, "_finalize_proposal",
        lambda home, **k: ProposalResult(
            ok=False, branch=None, pushed=False, pr_url=None, reason="no_open", detail="x"
        ),
    )
    with pytest.raises(tp.ProposalSubmissionError) as raised:
        _inv(tp.submit_proposal, title="t", rationale="r", lane="upgrade")
    assert raised.value.reason == "no_open"
    assert raised.value.lane == "upgrade"
    assert "no `upgrade` proposal is open" in str(raised.value)


def test_submit_tool_secret(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setattr(
        tp, "_finalize_proposal",
        lambda home, **k: ProposalResult(
            ok=False, branch="b", pushed=False, pr_url=None,
            reason="secret", detail="contains a secret-shaped token",
        ),
    )
    with pytest.raises(tp.ProposalSubmissionError, match="secret") as raised:
        _inv(tp.submit_proposal, title="t", rationale="r")
    assert raised.value.reason == "secret"
    assert raised.value.lane == "agent"


def test_submit_tool_requires_fields(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    with pytest.raises(tp.ProposalSubmissionError, match="required") as raised:
        _inv(tp.submit_proposal, title="", rationale="r")
    assert raised.value.reason == "invalid_arguments"


@pytest.mark.parametrize("lane", ["agent", "upgrade"])
def test_submit_tool_pr_failure_is_typed_for_every_lane(monkeypatch, tmp_path, lane) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setattr(
        tp, "_finalize_proposal",
        lambda home, **k: ProposalResult(
            ok=False,
            branch=f"{lane}/change",
            pushed=True,
            pr_url=None,
            reason="pr_open",
            detail="gh pr create failed: authentication required",
        ),
    )

    with pytest.raises(tp.ProposalSubmissionError) as raised:
        _inv(tp.submit_proposal, title="t", rationale="r", lane=lane)

    assert raised.value.reason == "pr_open"
    assert raised.value.lane == lane
    assert "gh pr create failed" in str(raised.value)


# ─── abandon ─────────────────────────────────────────────────────────


def test_abandon_tool(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setattr(tp, "list_open_proposals", lambda home, lane="agent": [("upgrade/z", tmp_path)])
    monkeypatch.setattr(tp, "_abandon_proposal", lambda home, lane="agent": True)
    out = _inv(tp.abandon_proposal, lane="upgrade")
    assert "upgrade/z" in out and "upgrade" in out and "bandon" in out


def test_abandon_tool_nothing_open(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setattr(tp, "list_open_proposals", lambda home, lane="agent": [])
    monkeypatch.setattr(tp, "_abandon_proposal", lambda home, lane="agent": False)
    assert "nothing to abandon" in _inv(tp.abandon_proposal, lane="upgrade").lower()


@pytest.fixture
def poller_runtime(monkeypatch, tmp_path):
    service = build_trigger_service_principal(
        canonical="poller:papers", trigger="poller", profile="research",
        tier=CapabilityTier.SCOPED_WITH_PROVENANCE,
        capabilities=("open_proposal", "submit_proposal", "abandon_proposal",
                      "read_file", "write_file", "edit_file"),
        roots=(tmp_path / "home/state/pollers/papers",), creation_path="test",
    )
    auth = create_auth_context(AgentEvent(
        trigger="poller", channel_id=service.canonical, source="poller",
        source_id="feed:item:42", service_principal=service.canonical,
        service_authority=service,
    ), enforce=True, ifc_labels=InformationFlowLabels(sources=(SourceLabel(
        principal=None, domain="public", resource_id="https://arxiv.org/abs/2609.00042",
        bridge_instance=None, sensitivity="public", source_kind="fetch_url",
        integrity="untrusted", integrity_effect="active_ingest",
    ),)))
    turn = TurnContext(
        turn_id="papers-turn-42", session_id=service.canonical, trigger="poller",
        channel_id=service.canonical, started_at=0, auth_context=auth,
    )
    monkeypatch.setattr(_context, "get_current_turn", lambda: turn)
    return ToolRuntime(
        state={}, context=auth, config={}, stream_writer=lambda _: None,
        tool_call_id="proposal-call", store=None,
    )


@pytest.fixture
def proposal_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / "state/wiki").mkdir(parents=True)
    (home / "state/wiki/paper.md").write_text("original\n")
    (home / ".gitignore").write_text("scratch/\n")
    for key, value in {
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.org",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.org",
        "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "commit.gpgsign",
        "GIT_CONFIG_VALUE_0": "false", "MIMIR_HOME": str(home),
    }.items():
        monkeypatch.setenv(key, value)
    upstream = tmp_path / "upstream.git"
    for args in (
        ("init", "--bare", "-q", "-b", "main", str(upstream)),
        ("init", "-q", "-b", "main"), ("add", "."),
        ("commit", "-q", "-m", "seed"), ("remote", "add", "origin", str(upstream)),
        ("push", "-q", "-u", "origin", "main"),
    ):
        subprocess.run(["git", *args], cwd=home, check=True, capture_output=True)
    return home


@pytest.mark.parametrize("finish", ["submit", "pr_open", "abandon"])
def test_poller_real_git_flow(monkeypatch, proposal_home, poller_runtime, finish):
    import mimir.proposals as core

    prs = []
    events = []

    def opener(home, branch, base, title, body):
        prs.append((title, body))
        return None if finish == "pr_open" else "https://example.org/pr/42"

    async def log(kind, **kwargs):
        events.append((kind, kwargs))

    monkeypatch.setattr(core, "_default_open_pr", opener)
    monkeypatch.setattr(tp, "log_event", log)
    result = _inv(tp.open_proposal, runtime=poller_runtime, source="https://paper.test/42")
    state = poller_runtime.context.poller_proposal_state
    assert state.active and state.scope.owner == "poller:papers"
    assert state.scope.origin_ref == "feed:item:42"
    assert state.scope.turn_id == "papers-turn-42"
    assert state.worktree == poller_worktree_path(proposal_home, state.scope)
    assert "state/wiki/" in result and "memory/core/" not in result
    scope = state.scope
    assert "Already open" in _inv(tp.open_proposal, runtime=poller_runtime, source=scope.source)
    assert state.scope == scope
    if finish == "abandon":
        assert "Abandoned" in _inv(tp.abandon_proposal, runtime=poller_runtime)
        assert not prs
    else:
        with pytest.raises(tp.ProposalSubmissionError) as caught:
            _inv(tp.submit_proposal, runtime=poller_runtime, title="Paper", rationale="Useful")
        assert caught.value.reason == "no_changes"
        assert "state/wiki/" in str(caught.value)
        assert state.active
        (state.worktree / "state/wiki/paper.md").write_text("researched draft\n")
        # Submit remains bound to the injected carrier even in a fork without a turn.
        monkeypatch.setattr(_context, "get_current_turn", lambda: None)
        if finish == "pr_open":
            with pytest.raises(tp.ProposalSubmissionError) as caught:
                _inv(tp.submit_proposal, runtime=poller_runtime, title="Paper", rationale="Useful")
            assert caught.value.reason == "pr_open"
        else:
            assert "https://example.org/pr/42" in _inv(
                tp.submit_proposal, runtime=poller_runtime, title="Paper", rationale="Useful",
            )
            assert events[0][1]["lane"] == "poller"
        assert scope.source in prs[0][0] and "Untrusted-ingest source" in prs[0][1]
    assert not state.active and not state.worktree.exists()
    assert state.scope == scope
    assert (proposal_home / "state/wiki/paper.md").read_text() == "original\n"


@pytest.mark.parametrize("tool,kwargs", [
    (tp.open_proposal, {"source": "paper:42"}),
    (tp.submit_proposal, {"title": "T", "rationale": "R"}),
    (tp.abandon_proposal, {}),
])
@pytest.mark.parametrize("denial", [
    "missing", "forged", "untrusted", "no_context", "capability", "upgrade", "unsupported",
])
def test_poller_authority_denials(monkeypatch, tmp_path, poller_runtime, tool, kwargs, denial):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))

    def forbidden(*args, **kwargs):
        pytest.fail("denied operation reached proposal core")

    for name in ("_open_proposal", "_finalize_proposal", "_abandon_proposal", "list_open_proposals"):
        monkeypatch.setattr(tp, name, forbidden)
    runtime = poller_runtime
    lane = "agent"
    if denial == "missing":
        runtime, lane = None, "poller"
    elif denial == "forged":
        runtime = SimpleNamespace(context=poller_runtime.context)
    elif denial == "untrusted":
        runtime = replace(runtime, context=replace(runtime.context, is_service=False))
    elif denial == "no_context":
        runtime = replace(runtime, context=None)
    elif denial == "capability":
        runtime = replace(runtime, context=replace(runtime.context, service_authority=replace(
            runtime.context.service_authority, capabilities=(),
        )))
    else:
        lane = "upgrade" if denial == "upgrade" else "unsupported"
    with pytest.raises(ToolPolicyRefusal):
        asyncio.run(tool.coroutine(runtime=runtime, lane=lane, **kwargs))


@pytest.mark.parametrize("invalid", ["source", "source_missing", "origin", "turn_missing", "turn_copy", "turn_empty"])
def test_poller_open_requires_source_and_exact_turn(monkeypatch, tmp_path, poller_runtime, invalid):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    runtime = poller_runtime
    if invalid == "origin":
        runtime = replace(runtime, context=replace(runtime.context, origin_ref=None))
        turn = replace(_context.get_current_turn(), auth_context=runtime.context)
        monkeypatch.setattr(_context, "get_current_turn", lambda: turn)
    elif invalid == "turn_missing":
        monkeypatch.setattr(_context, "get_current_turn", lambda: None)
    elif invalid == "turn_copy":
        turn = replace(_context.get_current_turn(), auth_context=replace(runtime.context))
        assert turn.auth_context == runtime.context and turn.auth_context is not runtime.context
        monkeypatch.setattr(_context, "get_current_turn", lambda: turn)
    elif invalid == "turn_empty":
        turn = replace(_context.get_current_turn(), turn_id="")
        monkeypatch.setattr(_context, "get_current_turn", lambda: turn)
    monkeypatch.setattr(tp, "_open_proposal", lambda *a, **k: pytest.fail("unbound open"))
    kwargs = {} if invalid == "source_missing" else {"source": "  " if invalid == "source" else "paper:42"}
    with pytest.raises(ToolPolicyRefusal):
        _inv(tp.open_proposal, runtime=runtime, **kwargs)
    assert not runtime.context.poller_proposal_state.active


@pytest.mark.parametrize("tool,kwargs", [
    (tp.submit_proposal, {"title": "T", "rationale": "R"}), (tp.abandon_proposal, {}),
])
@pytest.mark.parametrize("invalid", ["inactive", "owner", "origin", "worktree", "changed_path", "scope_type"])
def test_poller_active_state_binding(monkeypatch, tmp_path, poller_runtime, tool, kwargs, invalid):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    state = poller_runtime.context.poller_proposal_state
    state.scope = PollerProposalScope(
        "poller:other" if invalid == "owner" else "poller:papers",
        "papers-turn-42", "paper:42", "other:item" if invalid == "origin" else "feed:item:42",
    )
    state.worktree = poller_worktree_path(tmp_path, state.scope)
    if invalid == "worktree":
        state.worktree = tmp_path / "another"
    elif invalid == "changed_path":
        state.worktree.parent.mkdir(parents=True)
        state.worktree.symlink_to(tmp_path, target_is_directory=True)
    elif invalid == "scope_type":
        state.scope = SimpleNamespace(**vars(state.scope))
    state.active = invalid != "inactive"
    for name in ("_finalize_proposal", "_abandon_proposal", "list_open_proposals"):
        monkeypatch.setattr(tp, name, lambda *a, **k: pytest.fail("unbound mutation"))
    if invalid == "inactive":
        with pytest.raises(tp.ProposalSubmissionError) as caught:
            _inv(tool, runtime=poller_runtime, **kwargs)
        assert caught.value.reason == "no_open"
    else:
        with pytest.raises(ToolPolicyRefusal):
            _inv(tool, runtime=poller_runtime, **kwargs)


def test_poller_open_rejects_source_overwrite(monkeypatch, tmp_path, poller_runtime):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    state = poller_runtime.context.poller_proposal_state
    state.scope = PollerProposalScope("poller:papers", "papers-turn-42", "paper:42", "feed:item:42")
    state.worktree = poller_worktree_path(tmp_path, state.scope)
    state.active = True
    monkeypatch.setattr(tp, "_open_proposal", lambda *a, **k: pytest.fail("source overwrite reached core"))
    with pytest.raises(ToolPolicyRefusal, match="overwritten"):
        _inv(tp.open_proposal, runtime=poller_runtime, source="paper:other")
    assert poller_runtime.context.poller_proposal_state.scope.source == "paper:42"


@pytest.mark.parametrize("reason", [None, "exists"])
@pytest.mark.parametrize("invalid", ["worktree", "branch", "missing_directory", "symlink"])
def test_poller_open_verifies_returned_path(monkeypatch, tmp_path, poller_runtime, reason, invalid):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    scope = PollerProposalScope("poller:papers", "papers-turn-42", "paper:42", "feed:item:42")
    expected = poller_worktree_path(tmp_path, scope)
    expected.parent.mkdir(parents=True)
    if invalid == "symlink":
        expected.symlink_to(tmp_path, target_is_directory=True)
    elif invalid != "missing_directory":
        expected.mkdir()
    monkeypatch.setattr(tp, "_open_proposal", lambda *a, **k: OpenResult(
        ok=reason is None,
        branch="poller/other/turn" if invalid == "branch" else poller_branch_name(scope),
        worktree=tmp_path if invalid == "worktree" else expected, reason=reason,
    ))
    with pytest.raises(ToolPolicyRefusal, match="returned worktree"):
        _inv(tp.open_proposal, runtime=poller_runtime, source="paper:42")
    assert not poller_runtime.context.poller_proposal_state.active


def test_poller_concurrent_open_is_refused(monkeypatch, tmp_path, poller_runtime):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    state = poller_runtime.context.poller_proposal_state
    monkeypatch.setattr(tp, "_open_proposal", lambda *a, **k: pytest.fail("concurrent open reached core"))
    state._operation_lock.acquire()
    try:
        with pytest.raises(ToolPolicyRefusal, match="in progress"):
            _inv(tp.open_proposal, runtime=poller_runtime, source="paper:42")
    finally:
        if state._operation_lock.locked():
            state._operation_lock.release()
    assert not state.active


def test_proposal_runtime_is_injected():
    for tool in (tp.open_proposal, tp.submit_proposal, tp.abandon_proposal):
        assert "runtime" not in tool.tool_call_schema.model_json_schema()["properties"]


@pytest.mark.parametrize("finish", ["submit", "pr_open", "abandon"])
def test_poller_state_stays_active_until_worktree_removed(
    monkeypatch, tmp_path, poller_runtime, finish,
):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    state = poller_runtime.context.poller_proposal_state
    state.scope = PollerProposalScope("poller:papers", "papers-turn-42", "paper:42", "feed:item:42")
    state.worktree = poller_worktree_path(tmp_path, state.scope)
    state.worktree.mkdir(parents=True)
    state.active = True
    if finish == "abandon":
        monkeypatch.setattr(tp, "_abandon_proposal", lambda *a, **k: True)
        _inv(tp.abandon_proposal, runtime=poller_runtime)
    else:
        monkeypatch.setattr(tp, "_finalize_proposal", lambda *a, **k: ProposalResult(
            ok=finish == "submit", branch="branch", pushed=True,
            pr_url="https://example.org/pr/42" if finish == "submit" else None,
            reason=None if finish == "submit" else "pr_open",
        ))

        async def log(*args, **kwargs):
            pass

        monkeypatch.setattr(tp, "log_event", log)
        if finish == "pr_open":
            with pytest.raises(tp.ProposalSubmissionError):
                _inv(tp.submit_proposal, title="T", rationale="R", runtime=poller_runtime)
        else:
            _inv(tp.submit_proposal, title="T", rationale="R", runtime=poller_runtime)
    assert state.active and state.worktree.is_dir()


def test_auth_context_proposal_state_defaults_are_independent(poller_runtime):
    auth = create_auth_context(AgentEvent(trigger="user_message", channel_id="test"), enforce=True)
    assert auth.poller_proposal_state is not poller_runtime.context.poller_proposal_state
    assert auth.poller_proposal_state.scope is None
    assert auth.poller_proposal_state.worktree is None
    assert not auth.poller_proposal_state.active


@pytest.mark.parametrize("invalid", [
    "forged_runtime", "missing_runtime", "missing_context", "forged_context",
    "untrusted_service", "untrusted_poller", "profile", "capability", "upgrade", "unsupported",
])
def test_poller_direct_authority_guard(monkeypatch, poller_runtime, invalid):
    import mimir.access_control as access

    runtime = poller_runtime
    auth = runtime.context
    service = auth.service_authority
    lane = "agent"
    if invalid == "forged_runtime":
        runtime = SimpleNamespace(context=auth)
    elif invalid == "missing_runtime":
        runtime, lane = None, "poller"
    elif invalid == "missing_context":
        runtime = replace(runtime, context=None)
    elif invalid == "forged_context":
        runtime = replace(runtime, context=SimpleNamespace(**vars(auth)))
    elif invalid == "untrusted_service":
        auth = replace(auth, trigger="user_message", canonical_principal="other", is_service=True)
        runtime = replace(runtime, context=auth)
        service = None
    elif invalid == "untrusted_poller":
        auth = replace(auth, is_service=False)
        runtime = replace(runtime, context=auth)
        service = None
    elif invalid == "profile":
        service = replace(service, authority_profile="github")
    elif invalid == "capability":
        service = replace(service, capabilities=())
    else:
        lane = invalid
    # Isolate the tool's use of the resolver from concurrent access-control mutations.
    monkeypatch.setattr(access, "get_trusted_service_from_auth_context", lambda context: service if context is auth else None)
    with pytest.raises(ToolPolicyRefusal):
        tp._poller_context(runtime, lane, "open_proposal")


def test_poller_failed_open_never_activates(monkeypatch, tmp_path, poller_runtime):
    scope = PollerProposalScope("poller:papers", "papers-turn-42", "paper:42", "feed:item:42")
    expected = poller_worktree_path(tmp_path, scope)
    expected.mkdir(parents=True)
    monkeypatch.setattr(tp, "_open_proposal", lambda *a, **k: OpenResult(
        ok=False, branch=poller_branch_name(scope), worktree=expected, reason="error",
    ))
    tp._run_poller(poller_runtime.context, tmp_path, "open_proposal", source="paper:42")
    assert not poller_runtime.context.poller_proposal_state.active


def test_poller_default_open_routes_exact_scope(monkeypatch, tmp_path, poller_runtime):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    scope = PollerProposalScope("poller:papers", "papers-turn-42", "paper:42", "feed:item:42")
    expected = poller_worktree_path(tmp_path, scope)
    expected.mkdir(parents=True)
    calls = []

    def open_(home, **kwargs):
        calls.append(kwargs)
        return OpenResult(ok=True, branch=poller_branch_name(scope), worktree=expected)

    monkeypatch.setattr(tp, "_open_proposal", open_)
    _inv(tp.open_proposal, runtime=poller_runtime, source="paper:42")
    assert calls == [{"lane": "poller", "poller": scope}]
    assert poller_runtime.context.poller_proposal_state.active


def test_poller_rejects_forged_state_carrier(monkeypatch, tmp_path, poller_runtime):
    state = poller_runtime.context.poller_proposal_state
    state.scope = PollerProposalScope("poller:papers", "papers-turn-42", "paper:42", "feed:item:42")
    state.worktree = poller_worktree_path(tmp_path, state.scope)
    state.worktree.mkdir(parents=True)
    state.active = True
    auth = replace(poller_runtime.context, poller_proposal_state=SimpleNamespace(**vars(state)))
    monkeypatch.setattr(tp, "_finalize_proposal", lambda *a, **k: pytest.fail("forged state reached core"))
    with pytest.raises(ToolPolicyRefusal, match="invalid poller state"):
        tp._run_poller(auth, tmp_path, "submit_proposal", title="T", rationale="R")


@pytest.mark.parametrize("operation", ["submit_proposal", "abandon_proposal"])
def test_poller_routes_retained_scope_without_ambient_turn(monkeypatch, tmp_path, poller_runtime, operation):
    state = poller_runtime.context.poller_proposal_state
    state.scope = PollerProposalScope("poller:papers", "papers-turn-42", "paper:42", "feed:item:42")
    state.worktree = poller_worktree_path(tmp_path, state.scope)
    state.worktree.mkdir(parents=True)
    state.active = True
    scope = state.scope
    monkeypatch.setattr(_context, "get_current_turn", lambda: None)
    calls = []

    def finish(home, **kwargs):
        calls.append(kwargs)
        state.worktree.rmdir()
        return True

    monkeypatch.setattr(tp, "_finalize_proposal", finish)
    monkeypatch.setattr(tp, "_abandon_proposal", finish)
    tp._run_poller(poller_runtime.context, tmp_path, operation, source="overwrite", title="T", rationale="R")
    assert calls[0]["poller"] is scope
    assert calls[0]["lane"] == "poller"
    assert scope.source == "paper:42"
    assert not state.active


def test_poller_proposal_middleware_defaults_and_backend_reads(
    proposal_home, poller_runtime, monkeypatch,
):
    from mimir.access_control import ToolRegistry, resolve_trigger_service_write_target
    from mimir.read_policy import is_current_service_scoped_read_path
    from mimir.tools.budget_gate import _validated_arguments
    import mimir.proposals as core

    auth = poller_runtime.context
    calls = []
    monkeypatch.setattr(core, "_default_open_pr", lambda *args: calls.append(args) or "https://example.org/pr/42")

    async def log(*args, **kwargs):
        pass

    monkeypatch.setattr(tp, "log_event", log)

    def invoke(tool, **args):
        request = SimpleNamespace(tool=tool, tool_call={"name": tool.name, "args": args})
        validated = _validated_arguments(request)
        assert validated["lane"] == "agent"
        decision = ToolRegistry().authorize_tool(tool.name, auth, enforce=True, arguments=validated)
        assert decision.allowed, decision.reason
        return _inv(tool, runtime=poller_runtime, **validated)

    # The middleware materializes the agent default; the trusted tool must map it
    # to poller without granting access to the agent's protected surfaces.
    invoke(tp.open_proposal, source="https://arxiv.org/abs/2609.00042")
    state = auth.poller_proposal_state
    page = state.worktree / "state/wiki/paper.md"
    assert is_current_service_scoped_read_path(page)
    policy = auth.service_authority.sink_policy_for("write_file")
    decision = ToolRegistry().authorize_tool("write_file", auth, enforce=True, target_channel=str(page))
    assert decision.allowed, decision.reason
    resolved = resolve_trigger_service_write_target(str(page), policy.destination, auth_context=auth)
    resolved.write_text("Research notes\n")
    assert "https://example.org/pr/42" in invoke(tp.submit_proposal, title="Paper insight", rationale="Research notes")
    assert calls[0][1].startswith("poller/papers/papers-turn-42-")
    assert "poller:papers" in calls[0][3] and "2609.00042" in calls[0][3]
    assert "Untrusted-ingest" in calls[0][4]
    assert (proposal_home / "state/wiki/paper.md").read_text() == "original\n"
    assert not is_current_service_scoped_read_path(page)

    # Abandon also receives the materialized default and reaches only this scope.
    invoke(tp.open_proposal, source="https://arxiv.org/abs/2609.00042")
    assert "Abandoned" in invoke(tp.abandon_proposal)
