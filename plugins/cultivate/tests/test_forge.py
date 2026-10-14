"""Forge compatibility surface until the host's coding registration is retired."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from langchain.tools import ToolRuntime

from mimir import access_control
from mimir.forge import PullRequestProjection
from mimir.models import (
    AuthContext, InformationFlowLabels, RepoPRAction, RepoPRActionScope,
    RepoPRScopeRegistry, RepoReviewState,
)
from mimir.tools import forge as legacy
from mimir.tools.refusals import ToolPolicyRefusal
from mimir_cultivate import forge


def _runtime() -> ToolRuntime[AuthContext]:
    scope = RepoPRActionScope(
        provenance="poller_payload", canonical_repo="owner/repo",
        canonical_root="/tmp/repo", canonical_origin="ssh://forge.invalid/owner/repo",
        principal="reviewer", event_type="pr_review_requested",
        allowed_operations=frozenset(action.value for action in RepoPRAction),
        pr_number=17, head_repo="contributor/repo", head_remote="source",
        destination_ref="refs/heads/change", observed_head_sha="a" * 40,
        base_ref="main", observed_base_sha="b" * 40,
    )
    state = RepoReviewState(scope)
    context = AuthContext(
        principal="service:poller", canonical_principal="poller", roles=("service",),
        event_ingress=None, trigger="poller", channel_id="poller:forge",
        interactivity=None, enforcement_enabled=True,
        ifc_labels=InformationFlowLabels(),
        repo_pr_scope_registry=RepoPRScopeRegistry((state,)),
        repo_review_state=state, repo_pr_action_scope=scope,
    )
    return ToolRuntime(
        state={}, context=context, config={}, stream_writer=lambda _: None,
        tool_call_id="cultivate-forge-test", store=None,
    )


class _Forge:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def get_pull_request(self, scope: RepoPRActionScope) -> PullRequestProjection:
        self.calls.append(("metadata", scope))
        return PullRequestProjection(
            17, "Title", "open", "author", False, "main", "change",
            "a" * 40, True, "created", "updated",
        )

    def author_is_trusted(self, repository: str, author: str) -> bool:
        self.calls.append(("attest", (repository, author)))
        return False

    def add_pull_request_comment(self, scope: RepoPRActionScope, body: str):
        self.calls.append(("comment", body))
        from mimir.forge import CommentProjection
        return CommentProjection("1", "author", body, "now", "now")


def test_forge_surface_matches_all_legacy_call_contracts() -> None:
    assert len(forge.FORGE_TOOLS) == 21
    assert [item.name for item in forge.FORGE_TOOLS] == [item.name for item in legacy.FORGE_TOOLS]
    for wrapped, original in zip(forge.FORGE_TOOLS, legacy.FORGE_TOOLS):
        assert wrapped is not original
        assert wrapped.func is not original.func
        assert wrapped.args_schema is original.args_schema
        assert wrapped.response_format == original.response_format
        assert wrapped.return_direct == original.return_direct
        assert callable(wrapped.coroutine)


@pytest.mark.asyncio
async def test_plugin_call_site_preserves_scope_head_and_untrusted_taint() -> None:
    client = _Forge()
    legacy.set_forge_client(client)
    runtime = _runtime()
    metadata = next(tool for tool in forge.FORGE_TOOLS if tool.name == "pr_metadata")
    comment = next(tool for tool in forge.FORGE_TOOLS if tool.name == "pr_comment")
    token = access_control.begin_protected_result_capture()
    try:
        result = await metadata.coroutine("owner/repo", 17, runtime=runtime)
    finally:
        provenance = access_control.end_protected_result_capture(token)
    assert result["title"] == "Title"
    assert client.calls == [
        ("metadata", runtime.context.repo_pr_action_scope),
        ("attest", ("owner/repo", "author")),
    ]
    assert provenance.sources[0].resource_id == "owner/repo#pull/17@" + "a" * 40
    assert provenance.sources[0].integrity == "untrusted"
    try:
        assert comment.func("owner/repo", 17, "posted", runtime=runtime)["body"] == "posted"
        with pytest.raises(ToolPolicyRefusal, match="not configured"):
            comment.func("other/repo", 18, "no", runtime=runtime)
        assert [call for call in client.calls if call[0] == "comment"] == [("comment", "posted")]
    finally:
        legacy.set_forge_client(None)


def test_disabled_provider_is_not_imported_by_host() -> None:
    program = (
        "import json, sys; from mimir.plugins.manager import load_plugins; "
        "manager = load_plugins(enable=sys.argv[1]); "
        "print(json.dumps([p.candidate.name for p in manager.plugins])); "
        "print('mimir_cultivate' in sys.modules)"
    )
    for selection, names, imported in (("", [], False), ("cultivate", ["cultivate"], True)):
        env = dict(os.environ, MIMIR_PLUGINS=selection)
        result = subprocess.run(
            [sys.executable, "-c", program, selection], env=env,
            capture_output=True, text=True, check=True,
        )
        actual_names, actual_imported = result.stdout.strip().splitlines()
        assert json.loads(actual_names) == names
        assert (actual_imported == "True") is imported
