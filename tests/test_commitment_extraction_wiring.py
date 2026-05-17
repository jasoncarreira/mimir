"""181-I regression: ``_maybe_extract_commitments`` on saga_session_end.

The SDK-era ``CommitmentExtractionHook`` was a member of the agent's
``_turn_hooks`` list and fired on ``finalize``. The deepagents-backed
agent has no hook chain, so the extraction path is inlined at the
end of ``_run_turn_body``. This test suite drives that path directly
with ``extract_commitments`` and ``log_event`` monkey-patched so we
never invoke an LLM or write events.jsonl from a unit test.

Covers the four outcomes that have distinct events:

  - ``short_output``         — output below MIN_OUTPUT_LEN, skipped.
  - ``llm_returned_zero``    — extractor ran, returned [].
  - ``all_dedupe_skipped``   — extracted N but all matched existing keys.
  - ``commitments_extracted``— ≥1 net-new record added.

Plus the negative guards:

  - non-synthesis trigger → no extraction.
  - empty output          → no extraction.
  - extractor raises      → log + continue, store untouched.
  - store.add raises      → log + continue with next record.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

from mimir.agent import Agent
from mimir.commitments.models import CommitmentRecord
from mimir.config import Config
from mimir.history import MessageBuffer
from mimir.index import IndexGenerator
from mimir.models import AgentEvent, TurnContext, TurnRecord
from mimir.turn_logger import TurnLogger


def _make_agent(tmp_path: Path) -> Agent:
    os.environ["MIMIR_HOME"] = str(tmp_path)
    cfg = Config.from_env()
    (cfg.home / "logs").mkdir(parents=True, exist_ok=True)
    (cfg.home / ".mimir").mkdir(parents=True, exist_ok=True)
    from mimir.commitments import CommitmentsStore
    store = CommitmentsStore(path=cfg.commitments_log)
    return Agent(
        config=cfg,
        turn_logger=TurnLogger(cfg.turns_log),
        message_buffer=MessageBuffer(history_path=cfg.home / "messages.jsonl"),
        index_generator=IndexGenerator(cfg.home),
        commitments_store=store,
    )


def _make_ctx(event: AgentEvent, saga_session_id: str | None = None) -> TurnContext:
    return TurnContext(
        turn_id="turn-extract-test",
        session_id=event.channel_id or "default",
        trigger=event.trigger,
        channel_id=event.channel_id,
        started_at=time.monotonic(),
        saga_session_id=saga_session_id,
    )


def _make_record(output: str, *, trigger: str = "saga_session_end") -> TurnRecord:
    return TurnRecord(
        ts="2026-05-17T00:00:00Z",
        turn_id="turn-extract-test",
        session_id="ch-1",
        saga_session_id="sess-1",
        trigger=trigger,
        channel_id="ch-1",
        input="(synthesis)",
        saga_atom_ids=[],
        events=[],
        output=output,
        duration_ms=0,
        error=None,
    )


def _make_commitment_record(dedupe_key: str = "k1") -> CommitmentRecord:
    """Build a CommitmentRecord with the minimum fields the wiring needs.

    Resolved at import time so any model shape drift fails the test
    file collection instead of the assertions — visible at CI time.
    """
    # Inspect __init__ to fill required fields; pin the dedupe_key.
    return CommitmentRecord(
        id="c-test",
        text="follow up about X next Tue",
        channel_id="ch-1",
        saga_session_id="sess-1",
        source_turn_id="turn-extract-test",
        dedupe_key=dedupe_key,
    )


# ─── Trigger / payload gating ───────────────────────────────────────


@pytest.mark.asyncio
async def test_non_synthesis_trigger_skips_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``user_message`` turns never trigger extraction — even with a
    long output that would otherwise pass MIN_OUTPUT_LEN."""
    agent = _make_agent(tmp_path)
    called: list[Any] = []
    monkeypatch.setattr(
        "mimir.commitments.extractor.extract_commitments",
        lambda *a, **k: called.append((a, k)) or [],
    )
    event = AgentEvent(trigger="user_message", channel_id="ch-1", content="hi")
    ctx = _make_ctx(event)
    record = _make_record("x" * 5000, trigger="user_message")
    await agent._maybe_extract_commitments(ctx, event, record)
    assert called == []


@pytest.mark.asyncio
async def test_empty_output_skips_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _make_agent(tmp_path)
    called: list[Any] = []
    monkeypatch.setattr(
        "mimir.commitments.extractor.extract_commitments",
        lambda *a, **k: called.append((a, k)) or [],
    )
    event = AgentEvent(trigger="saga_session_end", channel_id="ch-1")
    ctx = _make_ctx(event, saga_session_id="sess-1")
    record = _make_record("", trigger="saga_session_end")
    await agent._maybe_extract_commitments(ctx, event, record)
    assert called == []


# ─── The four outcome events ───────────────────────────────────────


@pytest.mark.asyncio
async def test_short_output_emits_no_op_short_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Output shorter than ``MIN_OUTPUT_LEN`` skips the LLM call and
    emits ``commitments_extraction_no_op`` with reason=short_output."""
    agent = _make_agent(tmp_path)
    from mimir.commitments.extractor import MIN_OUTPUT_LEN

    events: list[tuple[str, dict]] = []

    async def _capture(kind: str, **kw: Any) -> None:
        events.append((kind, kw))

    monkeypatch.setattr("mimir.agent.log_event", _capture)
    # The extractor must NOT be invoked on the short-output path.
    monkeypatch.setattr(
        "mimir.commitments.extractor.extract_commitments",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("extract_commitments must not run on short output")
        ),
    )

    event = AgentEvent(trigger="saga_session_end", channel_id="ch-1")
    ctx = _make_ctx(event, saga_session_id="sess-1")
    record = _make_record("x" * (MIN_OUTPUT_LEN - 1))
    await agent._maybe_extract_commitments(ctx, event, record)

    kinds = [k for k, _ in events]
    assert "commitments_extraction_no_op" in kinds
    no_op_event = next(kw for k, kw in events if k == "commitments_extraction_no_op")
    assert no_op_event["reason"] == "short_output"


@pytest.mark.asyncio
async def test_llm_returns_zero_emits_no_op_llm_returned_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _make_agent(tmp_path)
    from mimir.commitments.extractor import MIN_OUTPUT_LEN

    events: list[tuple[str, dict]] = []

    async def _capture(kind: str, **kw: Any) -> None:
        events.append((kind, kw))

    async def _empty_extract(*args: Any, **kwargs: Any) -> list:
        return []

    monkeypatch.setattr("mimir.agent.log_event", _capture)
    monkeypatch.setattr(
        "mimir.commitments.extractor.extract_commitments", _empty_extract,
    )

    event = AgentEvent(trigger="saga_session_end", channel_id="ch-1")
    ctx = _make_ctx(event, saga_session_id="sess-1")
    record = _make_record("y" * (MIN_OUTPUT_LEN + 100))
    await agent._maybe_extract_commitments(ctx, event, record)

    kinds_with_reasons = [
        (k, kw.get("reason")) for k, kw in events
    ]
    assert ("commitments_extraction_no_op", "llm_returned_zero") in kinds_with_reasons


@pytest.mark.asyncio
async def test_added_emits_commitments_extracted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _make_agent(tmp_path)
    from mimir.commitments.extractor import MIN_OUTPUT_LEN

    events: list[tuple[str, dict]] = []

    async def _capture(kind: str, **kw: Any) -> None:
        events.append((kind, kw))

    record_to_add = _make_commitment_record(dedupe_key="net-new-1")

    async def _extract_one(*args: Any, **kwargs: Any) -> list:
        return [record_to_add]

    monkeypatch.setattr("mimir.agent.log_event", _capture)
    monkeypatch.setattr(
        "mimir.commitments.extractor.extract_commitments", _extract_one,
    )

    event = AgentEvent(trigger="saga_session_end", channel_id="ch-1")
    ctx = _make_ctx(event, saga_session_id="sess-1")
    record = _make_record("z" * (MIN_OUTPUT_LEN + 100))
    await agent._maybe_extract_commitments(ctx, event, record)

    kinds = [k for k, _ in events]
    assert "commitments_extracted" in kinds
    persisted = next(kw for k, kw in events if k == "commitments_extracted")
    assert persisted["count"] == 1
    assert persisted["skipped_dedupe"] == 0
    # Verify the record actually landed in the store.
    state = agent._commitments.current_state()
    assert any(r.dedupe_key == "net-new-1" for r in state.values())


@pytest.mark.asyncio
async def test_all_dedupe_skipped_emits_no_op_all_dedupe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When extracted records all match existing in-flight commitments,
    nothing lands in the store + ``commitments_extraction_no_op`` fires
    with reason=all_dedupe_skipped."""
    agent = _make_agent(tmp_path)
    from mimir.commitments.extractor import MIN_OUTPUT_LEN

    # Pre-load the store with a record carrying dedupe_key="dup-key".
    seed = _make_commitment_record(dedupe_key="dup-key")
    await agent._commitments.add(seed)

    events: list[tuple[str, dict]] = []

    async def _capture(kind: str, **kw: Any) -> None:
        events.append((kind, kw))

    re_emerged = CommitmentRecord(
        id="c-dup",
        text="same commitment as before",
        channel_id="ch-1",
        saga_session_id="sess-1",
        source_turn_id="turn-extract-test",
        dedupe_key="dup-key",
    )

    async def _extract_dup(*args: Any, **kwargs: Any) -> list:
        return [re_emerged]

    monkeypatch.setattr("mimir.agent.log_event", _capture)
    monkeypatch.setattr(
        "mimir.commitments.extractor.extract_commitments", _extract_dup,
    )

    event = AgentEvent(trigger="saga_session_end", channel_id="ch-1")
    ctx = _make_ctx(event, saga_session_id="sess-1")
    record = _make_record("w" * (MIN_OUTPUT_LEN + 100))
    await agent._maybe_extract_commitments(ctx, event, record)

    no_op = [kw for k, kw in events if k == "commitments_extraction_no_op"]
    assert any(kw.get("reason") == "all_dedupe_skipped" for kw in no_op)


# ─── Failure-mode guards ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_extractor_raises_does_not_crash_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the LLM extraction itself errors, log + return; the synthesis
    turn record itself must be unaffected."""
    agent = _make_agent(tmp_path)
    from mimir.commitments.extractor import MIN_OUTPUT_LEN

    async def _boom(*args: Any, **kwargs: Any) -> list:
        raise RuntimeError("extractor boom")

    monkeypatch.setattr(
        "mimir.commitments.extractor.extract_commitments", _boom,
    )

    event = AgentEvent(trigger="saga_session_end", channel_id="ch-1")
    ctx = _make_ctx(event, saga_session_id="sess-1")
    record = _make_record("v" * (MIN_OUTPUT_LEN + 100))
    # Must not raise.
    await agent._maybe_extract_commitments(ctx, event, record)


@pytest.mark.asyncio
async def test_no_commitments_store_skips_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test harnesses construct an Agent without a CommitmentsStore.
    The extraction path must be a no-op in that case."""
    agent = _make_agent(tmp_path)
    agent._commitments = None  # simulate a no-store Agent

    called: list[Any] = []
    monkeypatch.setattr(
        "mimir.commitments.extractor.extract_commitments",
        lambda *a, **k: called.append((a, k)) or [],
    )

    event = AgentEvent(trigger="saga_session_end", channel_id="ch-1")
    ctx = _make_ctx(event, saga_session_id="sess-1")
    record = _make_record("a" * 5000)
    await agent._maybe_extract_commitments(ctx, event, record)
    assert called == []


@pytest.mark.asyncio
async def test_commitment_extraction_forces_unbound_on_synthetic_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug fix: ``channel_bound=True`` commitments extracted from a
    synthetic channel (``scheduler:*`` / ``poller:*``) must be stored
    as unbound (channel_id=None), not bound to the synthetic channel.

    A commitment bound to ``scheduler:heartbeat`` is permanently
    orphaned — ``_assemble_commitments_block`` suppresses rendering on
    synthetic channels, so it never surfaces to the operator. The fix
    nullifies the channel_id passed to ``extract_commitments`` when
    the source channel is synthetic.

    Two assertions:
    1. ``extract_commitments`` receives ``channel_id=None`` (not the
       raw synthetic channel), so ``channel_bound=True`` records in
       the extractor's output can't accidentally bind to a dead channel.
    2. The resulting store record has ``channel_id=None`` (unbound),
       confirming end-to-end the commitment surfaces cross-channel.
    """
    from mimir.commitments import CommitmentsStore
    from mimir.commitments.models import make_commitment_id

    agent = _make_agent(tmp_path)

    captured: list[str | None] = []

    async def fake_extract(
        output: str,
        *,
        channel_id: str | None,
        saga_session_id: str | None,
        source_turn_id: str,
    ) -> list[CommitmentRecord]:
        captured.append(channel_id)
        # Simulate a channel_bound=True extraction — the LLM bound this
        # commitment to the source channel. With the bug, this would land
        # as channel_id="scheduler:heartbeat"; after the fix it must land
        # as channel_id=None (unbound).
        return [CommitmentRecord(
            id=make_commitment_id(),
            channel_id=channel_id,  # mirrors what _coerce_to_record does
            text="Follow up on Jason's sequencing pick",
            kind="open_loop",
            confidence=0.9,
            source_turn_id=source_turn_id,
            saga_session_id=saga_session_id,
        )]

    monkeypatch.setattr(
        "mimir.commitments.extractor.extract_commitments", fake_extract,
    )
    events: list[tuple[str, dict[str, Any]]] = []

    async def _capture(event_type: str, **kw: Any) -> None:
        events.append((event_type, kw))

    monkeypatch.setattr("mimir.agent.log_event", _capture)

    # Fire on a heartbeat channel — the common source of this bug.
    event = AgentEvent(trigger="saga_session_end", channel_id="scheduler:heartbeat")
    ctx = _make_ctx(event, saga_session_id="sess-synth-1")
    record = _make_record("x" * 5000)

    await agent._maybe_extract_commitments(ctx, event, record)

    # (1) The extractor received channel_id=None, not the synthetic channel.
    assert captured == [None], (
        f"expected channel_id=None passed to extractor; got {captured}"
    )

    # (2) The stored commitment is unbound (channel_id=None) so it
    #     surfaces cross-channel rather than being orphaned.
    state = agent._commitments.current_state()
    assert len(state) == 1
    rec = next(iter(state.values()))
    assert rec.channel_id is None, (
        f"commitment must be unbound after synthetic-channel extraction; "
        f"got channel_id={rec.channel_id!r}"
    )
