"""Actual review turn seals the server digest after its model finishes."""
import re
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from mimir import access_control as ac, commitment_proposals as cp
from mimir.commitments import CommitmentRecord, CommitmentsStore, make_commitment_id
from mimir.models import AgentEvent
from mimir.tools import registry
from tests.test_agent import _build_agent, _FakeAgent


@pytest.mark.asyncio
@pytest.mark.parametrize("enforced", [False, True])
async def test_real_review_turn_seals_batch_only_after_model_finishes(tmp_path, monkeypatch, enforced):
    home = tmp_path / "home"
    monkeypatch.setenv("MIMIR_HOME", str(home))
    store = CommitmentsStore(path=home / ".mimir/commitments.jsonl")
    record = await store.add(CommitmentRecord(id=make_commitment_id(), channel_id="discord-1", text="Ship fix", owner_principal="operator"))
    monkeypatch.setitem(registry._STATE, "commitments_store", store)
    messages = []

    async def send(channel, text, *, final):
        messages.append(text)
        return SimpleNamespace(sent=True)

    class Review(_FakeAgent):
        async def astream(self, state, *, config, context=None, stream_mode="values"):
            runtime = SimpleNamespace(context=context)
            assert record.id in await registry.commitment_list.coroutine(due_within_days=0, runtime=runtime)
            await registry.commitment_propose_closure.coroutine(commitment_id=record.id,
                action="complete", evidence="PR #42 merged", runtime=runtime)
            assert not await cp.post_review_digest(home, "discord-1", send)
            async for chunk in super().astream(state, config=config, context=context, stream_mode=stream_mode):
                yield chunk

    agent = _build_agent(tmp_path, fake_agent=Review([AIMessage(content="review done")]))
    agent._config.access_control_enforced = enforced
    monkeypatch.setitem(cp._BACKENDS, home, ("discord-1", store))
    result = await agent.run_turn(AgentEvent(trigger="scheduled_tick", source="scheduler",
        channel_id="scheduler:commitments-review", content="Review commitments",
        service_principal="scheduler", service_authority=ac.build_scheduled_tick_service_principal("commitments-review", home)),
        turn_id="actual-review")
    assert result.error is None
    assert await cp.post_review_digest(home, "discord-1", send)
    assert len(messages) == 1 and re.search(r"cb-[a-z2-7]{4}", messages[0])
    assert store.current_state()[record.id].status == "pending"
    assert not await cp.post_review_digest(home, "discord-1", send)
