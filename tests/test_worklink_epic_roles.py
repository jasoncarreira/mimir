from __future__ import annotations

import pytest

from mimir.worklink.epic_roles import DeepAgentsTaskSubagentInvoker
from mimir.worklink.orchestrator import WorklinkError
from mimir.worklink.review import (
    DecomposeReview,
    IntegrationValidation,
    SliceReview,
    WorkDecomposition,
)


def _patch_create_agent(monkeypatch, factory) -> None:
    import langchain.agents as la

    monkeypatch.setattr(la, "create_agent", factory)


def test_build_agents_compiles_one_runnable_per_role(tmp_path, monkeypatch) -> None:
    """Every review role compiles into a runnable with its response_format.

    Regression: the bridge used to hand raw config specs to DeepAgents'
    private _build_task_tool, which requires already-compiled ``runnable``
    specs and raised ``KeyError: 'runnable'`` on the first live epic decompose.
    """
    seen: dict[str, object] = {}

    def stub_create_agent(model, *, system_prompt, tools, middleware, name, response_format):
        assert system_prompt  # each role carries a prompt
        assert isinstance(tools, list)
        assert middleware  # read-only filesystem middleware attached
        seen[name] = response_format
        return f"runnable:{name}"

    _patch_create_agent(monkeypatch, stub_create_agent)
    invoker = DeepAgentsTaskSubagentInvoker(home=tmp_path, repo=tmp_path, model=object())

    agents = invoker._build_agents()

    assert set(agents) == {
        "work-decomposer",
        "decompose-reviewer",
        "per-slice-reviewer",
        "integration-validator",
    }
    assert seen["work-decomposer"] is WorkDecomposition
    assert seen["decompose-reviewer"] is DecomposeReview
    assert seen["per-slice-reviewer"] is SliceReview
    assert seen["integration-validator"] is IntegrationValidation


@pytest.mark.asyncio
async def test_invoker_returns_structured_response(tmp_path, monkeypatch) -> None:
    review = SliceReview(verdict="APPROVE", summary="ok")

    class StubAgent:
        def __init__(self) -> None:
            self.states: list[dict] = []

        async def ainvoke(self, state):
            self.states.append(state)
            return {"messages": [], "structured_response": review}

    stub = StubAgent()
    _patch_create_agent(monkeypatch, lambda *a, **k: stub)
    invoker = DeepAgentsTaskSubagentInvoker(home=tmp_path, repo=tmp_path, model=object())

    out = await invoker("per-slice-reviewer", "review this observed diff", SliceReview)

    assert out is review
    # The role received the prompt as a human message (not worker prose channels).
    assert stub.states and stub.states[0]["messages"][0].content == "review this observed diff"


@pytest.mark.asyncio
async def test_invoker_raises_without_structured_response(tmp_path, monkeypatch) -> None:
    class StubAgent:
        async def ainvoke(self, state):
            return {"messages": []}

    _patch_create_agent(monkeypatch, lambda *a, **k: StubAgent())
    invoker = DeepAgentsTaskSubagentInvoker(home=tmp_path, repo=tmp_path, model=object())

    with pytest.raises(WorklinkError, match="structured_response"):
        await invoker("per-slice-reviewer", "x", SliceReview)


@pytest.mark.asyncio
async def test_invoker_rejects_unknown_role(tmp_path, monkeypatch) -> None:
    _patch_create_agent(monkeypatch, lambda *a, **k: object())
    invoker = DeepAgentsTaskSubagentInvoker(home=tmp_path, repo=tmp_path, model=object())

    with pytest.raises(WorklinkError, match="unknown Worklink epic role"):
        await invoker("no-such-role", "x", SliceReview)
