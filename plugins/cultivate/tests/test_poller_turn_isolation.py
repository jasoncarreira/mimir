"""A poller review's untrusted tool output must die with its model thread."""

from __future__ import annotations

from dataclasses import replace

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from mimir.access_control import (
    ToolRegistry, build_trigger_service_principal, CapabilityTier,
    ScopedPRGrants, _SCOPED_PR_ISSUER, consume_scoped_pr_checkout,
    observe_scoped_pr_checkout,
)
from mimir.models import AgentEvent, InformationFlowLabels, SourceLabel
from mimir.plugins.manager import load_plugins
from mimir_cultivate.policy import RepoPRAction, RepoPRActionScope, RepoPRScopeRegistry, RepoReviewState
from mimir_cultivate.repository import create_pr_checkout_lease
from tests.test_agent import _FakeAgent, _FakeSessionManager, _build_agent
from tests.test_repo_tools import _repo_scope_and_state


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["github-activity", "github-ci-watch"])
async def test_poller_review_taint_and_model_history_are_per_event(
    tmp_path, monkeypatch, channel,
):
    from mimir import event_logger

    selected = load_plugins(enable="cultivate").policy
    events = []
    monkeypatch.setattr(event_logger, "log_event_sync", lambda name, **kw: events.append((name, kw)))
    sessions = _FakeSessionManager()
    url = "https://openjdk.org/source/module-info.java"
    secret = "UNTRUSTED_PREVIOUS_PR_TOOL_RESULT"
    scopes = {}
    for number in (360, 361):
        root = tmp_path / str(number)
        root.mkdir()
        _, _, template, _ = _repo_scope_and_state(root)
        scope = replace(template, pr_number=number, event_type="pr_review_requested")
        review_state = RepoReviewState(scope)
        lease_root = root / "review-leases"
        lease_root.mkdir()
        assert create_pr_checkout_lease(
            scope, owner=scope.principal, lease_root=lease_root, review_state=review_state,
        ) is not None
        scopes[number] = (scope, review_state, lease_root)

    class ReviewModel(_FakeAgent):
        def __init__(self):
            super().__init__([])
            self.threads = {}
            self.model_messages = []
            self.decisions = []
            self.states = []

        async def astream(self, state, *, config, context=None, stream_mode="values"):
            thread = config["configurable"]["thread_id"]
            messages = self.threads.get(thread, []) + list(state["messages"])
            self.model_messages.append(messages[:])
            self.states.append(context.ifc_state)
            number = len(self.model_messages)
            def read(resource, integrity):
                context.ifc_state.merge(InformationFlowLabels(sources=(SourceLabel(
                    principal="service:poller", domain="web" if integrity == "untrusted" else "github",
                    resource_id=resource, bridge_instance="fetch_url" if integrity == "untrusted" else "pr_diff",
                    sensitivity="internal", authorized_principals=frozenset({"service:poller"}),
                    integrity=integrity, integrity_effect="active_ingest",
                ),)))

            if number == 1:
                read(url, "untrusted")
                messages.append(ToolMessage(content=secret, tool_call_id="fetch_url-360"))
            read(f"owner/repo#pull/{359 + number}", "trusted")
            scope, review_state, lease_root = scopes[359 + number]
            monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(lease_root))
            grants = ScopedPRGrants(
                context.canonical_principal, (scope,), protocols=selected.scoped_actions,
                _issuer=_SCOPED_PR_ISSUER,
            )
            scoped_context = replace(
                context, repo_pr_scope_registry=RepoPRScopeRegistry((review_state,)),
                repo_review_state=review_state, repo_pr_action_scope=scope, scoped_pr_grants=grants,
            )
            token = consume_scoped_pr_checkout(scoped_context, scope, "cultivate")
            assert token is not None
            assert observe_scoped_pr_checkout(
                scoped_context, scope, review_state, token, "cultivate",
                selected.scoped_actions["cultivate"],
            )
            decision = ToolRegistry().authorize_tool(
                "pr_submit_review", scoped_context, enforce=True,
                arguments={"repository": "owner/repo", "pull_request": 359 + number,
                           "verdict": "comment", "body": "Review"},
                policy=selected,
            )
            self.decisions.append(decision)
            messages.append(AIMessage(content=f"review {number}"))
            self.threads[thread] = messages[:]
            yield {"messages": messages}

    graph = ReviewModel()
    agent = _build_agent(tmp_path, fake_agent=graph, session_manager=sessions)
    agent._config = replace(agent._config, plugin_manager=type("Selected", (), {
        "policy": selected, "plugins": (),
    })())
    authority = build_trigger_service_principal(
        canonical=f"poller:{channel}", trigger="poller", profile="github",
        tier=CapabilityTier.SCOPED_WITH_PROVENANCE, capabilities=("pr_submit_review",),
        channel_memory_directory=f"poller:{channel}", creation_path="test",
        policy=selected,
    )
    for number in (360, 361):
        await agent.run_turn(AgentEvent(
            trigger="poller", channel_id=authority.canonical, source="poller",
            service_principal=authority.canonical, service_authority=authority,
            content=f"Review owner/repo PR #{number}",
        ), turn_id=f"review-{number}")

    assert sessions.touch_calls == [authority.canonical] * 2
    assert graph.states[0] is not graph.states[1]
    assert graph.decisions[0].reason == "write_blocked_by_untrusted_ingest"
    assert graph.decisions[0].allowed is False
    assert graph.decisions[1].allowed is True, graph.decisions[1].reason
    assert secret in str(graph.threads[next(iter(graph.threads))])
    assert all(secret not in str(message) and "Review owner/repo PR #360" not in str(message)
               for message in graph.model_messages[1])
    assert len(graph.threads) == 2
    blocked = [fields for name, fields in events if name == "write_blocked_by_untrusted_ingest"]
    assert blocked and any(source["resource_id"] == url and source["bridge_instance"] == "fetch_url"
                           for source in blocked[0]["sources"])
