"""Exercise real Agent construction, native tools, spill readback and IFC."""

from __future__ import annotations

import inspect
import subprocess
from dataclasses import replace
from types import SimpleNamespace

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import Field

from mimir._context import get_current_turn
from mimir.access_control import (
    CapabilityTier, RepoBindingResolution, build_trigger_service_principal,
)
from mimir.models import AgentEvent, InformationFlowLabels, SourceLabel, NormalizedPullRequestSnapshot
from mimir.plugins.manager import load_plugins
from mimir_cultivate import forge
from tests.test_agent import _FakeSessionManager
from tests.test_agent_system_prompt_refresh import _make_agent
from tests.test_pr_checkout_lease import _repo_and_scope


@pytest.fixture(autouse=True)
def cultivate_policy():
    from mimir.plugins.core import policy_scope

    with policy_scope(load_plugins(enable="cultivate").policy):
        yield


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["github-activity"])
@pytest.mark.parametrize("external_ingest", [False, True])
async def test_real_poller_review_sequence(tmp_path, monkeypatch, channel, external_ingest):
    """No fake graph/checkpointer: only external forge/HTTP adapters are stubbed."""
    from mimir import event_logger
    from mimir.tools import web

    monkeypatch.setenv("MIMIR_PLUGINS", "cultivate")
    monkeypatch.setenv("MIMIR_MODEL_SPEC", "openai:test")
    monkeypatch.setenv("MIMIR_ACCESS_CONTROL_ENFORCED", "1")
    monkeypatch.setenv("MIMIR_GITHUB_SELF_LOGIN", "mimir-bot")
    monkeypatch.setenv("GITHUB_REPOS", "SquashMerge/trellis")
    selected = load_plugins(enable="cultivate")
    repo, template = _repo_and_scope(tmp_path)
    for number in (7, 8):
        subprocess.run(["git", "-C", str(repo), "push", "-q", "origin",
                        f"HEAD:refs/pull/{number}/head"], check=True)
    leases = tmp_path / "leases"
    leases.mkdir()
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(leases))
    monkeypatch.setattr("mimir.access_control._canonical_repo_binding_resolution", lambda _name:
        RepoBindingResolution((str(repo.resolve()), template.canonical_origin), (str(repo.resolve()),), 1))
    # Local bare origin substitutes for GitHub transport only; all scope, head,
    # checkout-author and provider-result binding checks remain real.
    monkeypatch.setattr(forge, "_github_origin_matches", lambda _: True)
    from mimir import access_control
    real_remote_repo = access_control._github_repo_from_remote
    monkeypatch.setattr(access_control, "_github_repo_from_remote", lambda remote:
        "squashmerge/trellis" if remote == template.canonical_origin else real_remote_repo(remote))
    events = []
    monkeypatch.setattr(event_logger, "log_event_sync", lambda name, **kw: events.append((name, kw)))
    posted = []
    first_marker = "FIRST_PR_ONLY_TOOL_RESULT"
    client = SimpleNamespace(
        get_pull_request_snapshot=lambda repository, number: NormalizedPullRequestSnapshot(
            repo="squashmerge/trellis", number=number, state="open", author="test",
            head_repo="squashmerge/trellis", head_remote="origin", head_ref=template.head_ref,
            head_sha=template.observed_head_sha, base_ref=template.base_ref,
            base_sha=template.observed_base_sha),
        get_pull_request=lambda scope: forge.PullRequestProjection(
            scope.pr_number, "Title", "open", "test", False, "main", "worklink/7",
            template.observed_head_sha, True, "created", "updated"),
        author_is_trusted=lambda *_: True,
        list_comments=lambda scope: (forge.CommentProjection(
            "1", "test", first_marker if scope.pr_number == 7 else "second PR", "now", "now"),),
        list_checks=lambda _: (),
        get_diff=lambda _: "needle " * 20000,
        get_file_content=lambda *_: "immutable file content",
        submit_review=lambda scope, verdict, body: (
            posted.append(scope.pr_number) or forge.ReviewProjection(
                "1", "mimir-bot", "COMMENTED", body, "now", scope.observed_head_sha)),
    )
    monkeypatch.setattr(forge, "_client_for_repository", lambda _: client)
    agent = _make_agent(tmp_path, monkeypatch)
    agent._config = replace(agent._config, plugin_manager=selected)
    agent._session_manager = _FakeSessionManager()
    skill = agent._config.home / ".mimir_builtin_skills" / "github" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: github\ndescription: GitHub\n---\nTrusted skill instructions\n")
    url = "https://api.github.com/repos/squashmerge/trellis/pulls/7"
    monkeypatch.setattr(web, "_validate_fetch_url", lambda _: None)
    def download(*, target_path, **kwargs):
        target_path.write_text("external untrusted body")
        import hashlib
        return {"status": 200, "bytes": 23, "content_type": "text/plain",
                "sha256": hashlib.sha256(target_path.read_bytes()).hexdigest()}
    monkeypatch.setattr(web, "_download_url_bytes", download)
    web.set_home(agent._config.home)
    capabilities = ("repo_checkout", "pr_metadata", "pr_comments", "pr_checks", "pr_diff",
                    "pr_file_content", "pr_submit_review", "pr_review_others", "read_file", "grep", "fetch_url")
    authority = build_trigger_service_principal(
        canonical=f"poller:{channel}", trigger="poller", profile="github",
        tier=CapabilityTier.CODE_EXECUTION, capabilities=capabilities,
        approved_urls=(url,), creation_path="test", policy=selected.policy, poller_owner="cultivate",
    )

    def call(name, args, identity):
        return AIMessage(content="", tool_calls=[{
            "name": name, "args": args, "id": identity, "type": "tool_call",
        }])

    def responses():
        for number in (7, 8):
            args = {"repository": "SquashMerge/trellis", "pull_request": number}
            for name in ("repo_checkout", "pr_metadata", "pr_comments", "pr_comments", "pr_checks", "pr_diff"):
                yield call(name, args, f"{name}-{number}")
            turn = get_current_turn()
            assert f"pr_diff-{number}" in turn.evicted_tool_result_ids
            path = f"/large_tool_results/{turn.turn_id}/pr_diff-{number}"
            yield call("grep", {"pattern": "needle", "path": path, "output_mode": "content"}, f"grep-{number}")
            yield call("read_file", {"file_path": path, "limit": 1}, f"offload-read-{number}")
            state = turn.auth_context.repo_pr_scope_registry.resolve("squashmerge/trellis", number)
            for i in range(4):
                yield call("read_file", {"file_path": str(state.checkout_lease.path / "file.txt")}, f"file-{number}-{i}")
            yield call("read_file", {"file_path": str(skill)}, f"skill-{number}")
            yield call("pr_file_content", {**args, "path": "file.txt"}, f"pr-file-{number}")
            if external_ingest and number == 7:
                yield call("fetch_url", {"url": url}, "external-fetch")
                import yaml
                result = next(m for m in reversed(model.requests[-1][1])
                              if isinstance(m, ToolMessage) and m.tool_call_id == "external-fetch")
                fetched_path = yaml.safe_load(result.content)["file_path"]
                yield call("read_file", {"file_path": fetched_path}, "external-read")
                yield call("pr_metadata", args, "trusted-after-fetch")
            yield call("pr_submit_review", {**args, "verdict": "comment", "body": "Reviewed"}, f"submit-{number}")
            yield AIMessage(content=f"finished-{number}")

    class Model(GenericFakeChatModel):
        requests: list = Field(default_factory=list)
        labels: list = Field(default_factory=list)

        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, *args, **kwargs):
            turn = get_current_turn()
            self.requests.append((turn.turn_id, list(messages)))
            labels = turn.auth_context.ifc_state.current()
            if len(self.requests) == 2:
                from mimir.access_control import pr_checkout_lease_attestation
                state = turn.auth_context.repo_pr_scope_registry.resolve("squashmerge/trellis", 7)
                attestation = pr_checkout_lease_attestation(turn.auth_context, state.action_scope, state.checkout_lease)
                assert attestation.ok, (attestation, turn.auth_context.ifc_state.pr_checkout_author_trust,
                                        state.action_scope.pull_request_author, events)
            self.labels.append((turn.turn_id, labels))
            if not self.requests[:-1]:
                assert not labels.has_untrusted_active_ingest, labels
            return super()._generate(messages, *args, **kwargs)

    model = Model(messages=iter(responses()), disable_streaming=True)
    agent._agent_model = model
    try:
        for number in (7, 8):
            item = {
                "event_type": "pr_opened", "repo": "SquashMerge/trellis", "number": number,
                "author": "test", "pr_author_is_trusted": True,
                "head_repo": "SquashMerge/trellis", "head_remote": "origin",
                "head_ref": template.head_ref, "head_sha": template.observed_head_sha,
                "base_ref": template.base_ref, "base_sha": template.observed_base_sha,
            }
            await agent.run_turn(AgentEvent(
                trigger="poller", channel_id=authority.canonical, source="poller",
                service_principal=authority.canonical, service_authority=authority,
                content=f"Review PR {number}", extra={"poller_name": channel, "items": [item]},
                # Match the host-attested collaborator event from fire_poller;
                # missing ingress attestation correctly defaults to untrusted.
                ifc_labels=InformationFlowLabels().with_source(SourceLabel(
                    principal=authority.canonical, domain="channel", resource_id=authority.canonical,
                    bridge_instance="poller", sensitivity="internal", source_kind="service",
                    authorized_principals=frozenset({authority.canonical}),
                    integrity="trusted", integrity_effect="active_ingest")),
            ), turn_id=f"review-{number}", saga_session_id="same-session")
    finally:
        web.set_home(None)

    assert agent._cached_system_prompt is not None  # built by the production builder
    assert agent._agent.checkpointer is None
    messages = [m for _, request in model.requests for m in request if isinstance(m, ToolMessage)]
    errors = {m.tool_call_id: m.content for m in messages if m.status == "error"}
    assert set(errors) == ({"submit-7"} if external_ingest else set()), (
        errors, [(turn, [(s.domain, s.resource_id, str(s.integrity)) for s in labels.sources
                         if s.has_untrusted_active_ingest]) for turn, labels in model.labels[:2]])
    assert posted == ([8] if external_ingest else [7, 8])
    assert any(first_marker in str(request) for turn, request in model.requests if turn == "review-7")
    assert all(first_marker not in str(request) and "finished-7" not in str(request)
               for turn, request in model.requests if turn == "review-8")
    assert all(not labels.has_untrusted_active_ingest for turn, labels in model.labels if turn == "review-8")
    blocked = [fields for name, fields in events if name == "write_blocked_by_untrusted_ingest"]
    if external_ingest:
        assert blocked and any(source["domain"] == "filesystem"
                               and "/attachments/fetch-cache/" in source["resource_id"]
                               for source in blocked[0]["sources"])
        assert any(labels.has_untrusted_active_ingest for turn, labels in model.labels if turn == "review-7")
    else:
        assert not blocked
        assert all(not labels.has_untrusted_active_ingest for _, labels in model.labels)


@pytest.mark.asyncio
async def test_real_review_sequence_detects_case_binding_regression(tmp_path, monkeypatch):
    from mimir import access_control

    helper = access_control._provider_pr_lease_result_is_bound
    source = inspect.getsource(helper)
    fixed = 'arguments["repository"].lower() == scope.canonical_repo.lower()'
    assert source.count(fixed) == 1
    namespace = dict(vars(access_control))
    exec(compile(source.replace(fixed, 'arguments.get("repository") == scope.canonical_repo'),
                 "<legacy-case-binding>", "exec"), namespace)
    monkeypatch.setattr(access_control, helper.__name__, namespace[helper.__name__])
    # Same production graph and real native tools, not a model-side history
    # emulation. The first post-checkout model request acquires untrusted ingest.
    with pytest.raises(AssertionError, match="unresolved-resource:protected_tool:repo_checkout"):
        await test_real_poller_review_sequence(tmp_path, monkeypatch, "github-activity", False)

