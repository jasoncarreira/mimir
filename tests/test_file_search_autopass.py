"""chainlink #139 (Sub A of #138) — auto-pass file_search in every-turn prompt.

Covers:
- the rendering helper ``_format_file_search_autopass`` produces a SAGA-
  atoms-block-style bullet list with ``[<path>:#<chunk_index> (score)]``
  labels.
- ``Agent._run_file_search_autopass`` gates correctly on
  ``Config.file_search_autopass_enabled`` (flag off → None), event kind
  (non-user_message → None), inbound length (< min_chars → None), and
  empty Indexer results (zero hits → None).
- ``prompts.build_turn_prompt`` slots the ``Possibly relevant files``
  block into the prompt as a sibling to the SAGA atoms block when
  ``file_block`` is non-None, and omits the section entirely otherwise.
- ``Agent._build_turn_prompt`` threads ``file_block`` end-to-end so an
  enabled autopass surfaces in the rendered prompt and a disabled one
  doesn't.

Indexer is constructed with the deterministic ``HashEmbedder`` so the
tests stay offline; the SAGA client is omitted because the autopass
path doesn't touch it.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from mimir.agent import Agent, _format_file_search_autopass
from mimir.config import Config
from mimir.event_logger import init_logger
from mimir.history import MessageBuffer
from mimir.index import IndexGenerator
from mimir.models import AgentEvent, TurnContext, make_process_session_id, make_turn_id
from mimir.search import HashEmbedder, Indexer, SearchResult
from mimir.turn_logger import TurnLogger


# ---- helpers -------------------------------------------------------------


def _cfg(tmp_path: Path, **overrides) -> Config:
    cfg = Config.from_env()
    return replace(
        cfg,
        home=tmp_path,
        file_search_autopass_enabled=overrides.get(
            "file_search_autopass_enabled", True,
        ),
        file_search_autopass_k=overrides.get("file_search_autopass_k", 5),
        file_search_autopass_min_chars=overrides.get(
            "file_search_autopass_min_chars", 20,
        ),
    )


def _seed_indexable(home: Path) -> None:
    """A few markdown files in memory/ + state/ so the Indexer has
    something to retrieve. Content is varied enough that a query for
    'quantum entanglement particles' lands a non-empty top-K."""
    (home / "memory" / "topics").mkdir(parents=True)
    (home / "state" / "transcripts").mkdir(parents=True)
    (home / "memory" / "topics" / "quantum.md").write_text(
        "<!-- desc: quantum mechanics notes -->\n# Quantum\n"
        "Quantum mechanics describes nature at atomic and subatomic scales. "
        "Particles exhibit wave-particle duality."
    )
    (home / "memory" / "topics" / "boids.md").write_text(
        "<!-- desc: boids flocking -->\n# Boids\n"
        "Boids is a flocking simulation by Craig Reynolds with three rules: "
        "separation, alignment, cohesion."
    )
    (home / "state" / "transcripts" / "kickoff.md").write_text(
        "<!-- desc: kickoff transcript -->\n# Kickoff\n"
        "We discussed quantum entanglement and particle physics at length."
    )


async def _build_agent(
    tmp_path: Path,
    *,
    indexer: Indexer | None,
    **cfg_overrides,
) -> Agent:
    cfg = _cfg(tmp_path, **cfg_overrides)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    init_logger(cfg.events_log, make_process_session_id())
    turn_logger = TurnLogger(cfg.turns_log)
    buf = MessageBuffer(history_path=cfg.home / "messages" / "chat_history.jsonl")
    indexes = IndexGenerator(cfg.home)
    return Agent(
        cfg,
        turn_logger,
        buf,
        indexes,
        indexer=indexer,
        saga_client=None,  # autopass path does not touch SAGA
        session_manager=None,
    )


def _ctx(channel_id: str = "discord-1") -> TurnContext:
    import time

    return TurnContext(
        turn_id=make_turn_id(),
        session_id=channel_id,
        trigger="user_message",
        channel_id=channel_id,
        started_at=time.monotonic(),
    )


# ---- _format_file_search_autopass ---------------------------------------


def test_format_file_search_autopass_empty_returns_placeholder():
    assert _format_file_search_autopass([]) == "(no files)"


def test_format_file_search_autopass_renders_saga_style_bullets():
    results = [
        SearchResult(
            path="memory/topics/quantum.md",
            scope="memory",
            chunk_index=0,
            score=0.7321,
            cosine=0.5,
            bm25=0.0,
            recency=1.0,
            snippet="Quantum mechanics describes nature at atomic scales.",
            description="quantum mechanics notes",
        ),
        SearchResult(
            path="state/transcripts/kickoff.md",
            scope="state",
            chunk_index=1,
            score=0.4111,
            cosine=0.3,
            bm25=0.1,
            recency=0.8,
            snippet="multi\nline\nsnippet content",
            description="kickoff transcript",
        ),
    ]
    rendered = _format_file_search_autopass(results)
    # One bullet per hit, SAGA-block shape: `- [<label> (score)] <preview>`.
    lines = rendered.splitlines()
    assert len(lines) == 2
    assert lines[0] == (
        "- [memory/topics/quantum.md:#0 (0.732)] "
        "Quantum mechanics describes nature at atomic scales."
    )
    # Newlines in the snippet are flattened into spaces, matching the
    # _format_atoms SAGA renderer.
    assert lines[1] == (
        "- [state/transcripts/kickoff.md:#1 (0.411)] multi line snippet content"
    )


def test_format_file_search_autopass_truncates_long_snippets():
    long_snippet = "x" * 500
    results = [
        SearchResult(
            path="memory/topics/big.md",
            scope="memory",
            chunk_index=0,
            score=0.5,
            cosine=0.5,
            bm25=0.0,
            recency=0.0,
            snippet=long_snippet,
            description=None,
        ),
    ]
    rendered = _format_file_search_autopass(results)
    # 240-char cap + ellipsis suffix, matching the SAGA-atoms formatter.
    assert rendered.endswith("…")
    bullet_body = rendered.split("] ", 1)[1]
    assert len(bullet_body) == 240 + 1  # 240 chars + ellipsis


# ---- Agent._run_file_search_autopass gating -----------------------------


@pytest.mark.asyncio
async def test_autopass_returns_none_when_flag_disabled(tmp_path: Path):
    _seed_indexable(tmp_path)
    indexer = Indexer(tmp_path, embedder=HashEmbedder())
    await indexer.start(run_initial_sweep=True, sweep_loop=False)
    try:
        agent = await _build_agent(
            tmp_path, indexer=indexer, file_search_autopass_enabled=False,
        )
        event = AgentEvent(
            trigger="user_message",
            channel_id="discord-1",
            content="tell me about quantum entanglement and particle physics",
            author="discord-99",
        )
        block = await agent._run_file_search_autopass(_ctx(), event)
        assert block is None
    finally:
        await indexer.stop()


@pytest.mark.asyncio
async def test_autopass_returns_none_for_scheduled_tick(tmp_path: Path):
    _seed_indexable(tmp_path)
    indexer = Indexer(tmp_path, embedder=HashEmbedder())
    await indexer.start(run_initial_sweep=True, sweep_loop=False)
    try:
        agent = await _build_agent(tmp_path, indexer=indexer)
        event = AgentEvent(
            trigger="scheduled_tick",
            channel_id="scheduler:heartbeat",
            content="quantum entanglement particle physics notes",
        )
        block = await agent._run_file_search_autopass(_ctx(), event)
        assert block is None
    finally:
        await indexer.stop()


@pytest.mark.asyncio
async def test_autopass_returns_none_for_short_message(tmp_path: Path):
    _seed_indexable(tmp_path)
    indexer = Indexer(tmp_path, embedder=HashEmbedder())
    await indexer.start(run_initial_sweep=True, sweep_loop=False)
    try:
        agent = await _build_agent(tmp_path, indexer=indexer)
        event = AgentEvent(
            trigger="user_message",
            channel_id="discord-1",
            content="ok ty",  # well under min_chars=20
            author="discord-99",
        )
        block = await agent._run_file_search_autopass(_ctx(), event)
        assert block is None
    finally:
        await indexer.stop()


@pytest.mark.asyncio
async def test_autopass_returns_none_when_indexer_not_wired(tmp_path: Path):
    """Tests / minimal Agents constructed without an Indexer must not
    crash when the flag is on — the hook just returns None."""
    agent = await _build_agent(tmp_path, indexer=None)
    event = AgentEvent(
        trigger="user_message",
        channel_id="discord-1",
        content="tell me about quantum entanglement and particle physics",
        author="discord-99",
    )
    block = await agent._run_file_search_autopass(_ctx(), event)
    assert block is None


@pytest.mark.asyncio
async def test_autopass_returns_none_when_indexer_yields_no_results(
    tmp_path: Path,
):
    """Empty index (no seeded files) → search returns []; autopass
    must render no block rather than emitting an empty section."""
    indexer = Indexer(tmp_path, embedder=HashEmbedder())
    # No _seed_indexable — index is empty after sweep.
    await indexer.start(run_initial_sweep=True, sweep_loop=False)
    try:
        agent = await _build_agent(tmp_path, indexer=indexer)
        event = AgentEvent(
            trigger="user_message",
            channel_id="discord-1",
            content="tell me about quantum entanglement and particle physics",
            author="discord-99",
        )
        block = await agent._run_file_search_autopass(_ctx(), event)
        assert block is None
    finally:
        await indexer.stop()


@pytest.mark.asyncio
async def test_autopass_renders_block_when_enabled_with_results(
    tmp_path: Path,
):
    """Happy path: flag on + user_message + ≥min_chars + non-empty
    index → the autopass hook returns a non-None block containing
    bullet rows for the top-K hits."""
    _seed_indexable(tmp_path)
    indexer = Indexer(tmp_path, embedder=HashEmbedder())
    await indexer.start(run_initial_sweep=True, sweep_loop=False)
    try:
        agent = await _build_agent(
            tmp_path, indexer=indexer, file_search_autopass_k=3,
        )
        event = AgentEvent(
            trigger="user_message",
            channel_id="discord-1",
            content="tell me about quantum entanglement and particle physics",
            author="discord-99",
        )
        block = await agent._run_file_search_autopass(_ctx(), event)
        assert block is not None
        lines = block.splitlines()
        # K=3 caps the bullet count even though the corpus has 3+ files.
        assert 1 <= len(lines) <= 3
        # Each line is a SAGA-shape bullet: `- [<path>:#<chunk_index> (score)] ...`
        for line in lines:
            assert line.startswith("- [")
            assert ":#" in line  # chunk-index marker
            assert "] " in line  # score-end → snippet separator
    finally:
        await indexer.stop()


@pytest.mark.asyncio
async def test_autopass_uses_rewritten_query_when_present_on_ctx(
    tmp_path: Path,
):
    """Follow-up to chainlink #139: when ``_pre_message_hook`` stashed
    SAGA's contextual rewrite on ``ctx.saga_rewritten_query``, the
    autopass must query the indexer with that expanded text — not the
    raw inbound ``event.content``. Keeps the SAGA atoms block and the
    Possibly relevant files block side-by-side in the prompt seeing
    consistent (and equally-expanded) queries; without this, a short
    ambiguous user message ("yes, that one") surfaces SAGA's expanded
    atoms next to a bag-of-3-tokens file_search result.

    Asserted by capturing the actual query string handed to
    ``Indexer.search``.
    """
    _seed_indexable(tmp_path)
    indexer = Indexer(tmp_path, embedder=HashEmbedder())
    await indexer.start(run_initial_sweep=True, sweep_loop=False)
    try:
        agent = await _build_agent(
            tmp_path, indexer=indexer, file_search_autopass_k=3,
        )
        event = AgentEvent(
            trigger="user_message",
            # Raw inbound is referential / short on signal — exactly the
            # case where contextual rewrite earns its keep. The autopass
            # MUST NOT use this string; it must use the rewritten one.
            content="yes that one",
            channel_id="discord-1",
            author="discord-99",
        )
        ctx = _ctx()
        # Simulate _pre_message_hook having stashed the rewrite.
        ctx.saga_rewritten_query = (
            "quantum entanglement particle physics — the topic discussed earlier"
        )

        # Capture the actual query handed to Indexer.search.
        captured: dict[str, str] = {}
        real_search = indexer.search

        async def _capture_search(query: str, *args, **kwargs):
            captured["query"] = query
            return await real_search(query, *args, **kwargs)

        indexer.search = _capture_search  # type: ignore[method-assign]
        try:
            await agent._run_file_search_autopass(ctx, event)
        finally:
            indexer.search = real_search  # type: ignore[method-assign]

        # The captured query is the rewrite, not the raw inbound.
        assert "query" in captured, "Indexer.search was not called"
        assert captured["query"] == ctx.saga_rewritten_query
        assert captured["query"] != event.content
    finally:
        await indexer.stop()


@pytest.mark.asyncio
async def test_autopass_falls_back_to_raw_content_when_no_rewrite(
    tmp_path: Path,
):
    """The complement of the rewrite-path test: when
    ``ctx.saga_rewritten_query`` is None (rewrite disabled, didn't
    fire, or was a no-op), the autopass uses the raw ``event.content``
    as it did before the follow-up — same behavior as the original
    chainlink #139 implementation.
    """
    _seed_indexable(tmp_path)
    indexer = Indexer(tmp_path, embedder=HashEmbedder())
    await indexer.start(run_initial_sweep=True, sweep_loop=False)
    try:
        agent = await _build_agent(
            tmp_path, indexer=indexer, file_search_autopass_k=3,
        )
        event = AgentEvent(
            trigger="user_message",
            channel_id="discord-1",
            content="tell me about quantum entanglement and particle physics",
            author="discord-99",
        )
        ctx = _ctx()
        # ctx.saga_rewritten_query stays None (its dataclass default) —
        # _pre_message_hook didn't fire / saga didn't carry a rewrite.

        captured: dict[str, str] = {}
        real_search = indexer.search

        async def _capture_search(query: str, *args, **kwargs):
            captured["query"] = query
            return await real_search(query, *args, **kwargs)

        indexer.search = _capture_search  # type: ignore[method-assign]
        try:
            await agent._run_file_search_autopass(ctx, event)
        finally:
            indexer.search = real_search  # type: ignore[method-assign]

        # Raw inbound (stripped) is what the indexer got.
        assert captured.get("query") == event.content.strip()
    finally:
        await indexer.stop()


@pytest.mark.asyncio
async def test_autopass_respects_top_k_override(tmp_path: Path):
    """Bumping ``file_search_autopass_k`` is plumbed all the way to
    ``Indexer.search``, so a higher K can surface more hits."""
    _seed_indexable(tmp_path)
    indexer = Indexer(tmp_path, embedder=HashEmbedder())
    await indexer.start(run_initial_sweep=True, sweep_loop=False)
    try:
        agent = await _build_agent(
            tmp_path, indexer=indexer, file_search_autopass_k=10,
        )
        event = AgentEvent(
            trigger="user_message",
            channel_id="discord-1",
            content="quantum entanglement particle physics flocking simulation",
            author="discord-99",
        )
        block = await agent._run_file_search_autopass(_ctx(), event)
        assert block is not None
        # Corpus is 3 files → at most 3 chunks (each file short → one chunk);
        # the K=10 override doesn't crash and surfaces every available hit.
        assert len(block.splitlines()) >= 1
    finally:
        await indexer.stop()


# ---- prompts.build_turn_prompt rendering --------------------------------


def test_turn_prompt_includes_file_block_when_provided():
    from mimir.prompts import build_turn_prompt

    event = AgentEvent(
        trigger="user_message",
        channel_id="discord-1",
        content="hi",
        author="discord-99",
    )
    file_block = "- [memory/topics/quantum.md:#0 (0.732)] Quantum mechanics…"
    prompt = build_turn_prompt(event, file_block=file_block)
    assert "## Possibly relevant files" in prompt
    assert "memory/topics/quantum.md:#0" in prompt


def test_turn_prompt_omits_file_block_when_none():
    from mimir.prompts import build_turn_prompt

    event = AgentEvent(
        trigger="user_message",
        channel_id="discord-1",
        content="hi",
        author="discord-99",
    )
    prompt = build_turn_prompt(event)  # no file_block
    assert "Possibly relevant files" not in prompt


def test_turn_prompt_renders_file_block_after_saga_block():
    """The autopass block sits next to the SAGA atoms block. The SAGA
    block comes first (existing behavior), the files block right after
    — both look like retrieval surfaces to the agent and reading them
    in order keeps the prompt scannable."""
    from mimir.prompts import build_turn_prompt

    event = AgentEvent(
        trigger="user_message",
        channel_id="discord-1",
        content="hi",
        author="discord-99",
    )
    saga_block = "- [observation/medium (0.9)] alice prefers terse"
    file_block = "- [memory/topics/quantum.md:#0 (0.7)] Quantum mechanics…"
    prompt = build_turn_prompt(
        event, saga_block=saga_block, file_block=file_block,
    )
    saga_idx = prompt.index("## Possibly relevant memories (from SAGA)")
    file_idx = prompt.index("## Possibly relevant files")
    assert saga_idx < file_idx, (
        f"SAGA block ({saga_idx}) should land before files block ({file_idx})"
    )


# ---- Config env wiring --------------------------------------------------


def test_config_default_disables_autopass(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MIMIR_FILE_SEARCH_AUTOPASS_ENABLED", raising=False)
    cfg = Config.from_env()
    assert cfg.file_search_autopass_enabled is False
    assert cfg.file_search_autopass_k == 5
    assert cfg.file_search_autopass_min_chars == 20


def test_config_reads_autopass_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MIMIR_FILE_SEARCH_AUTOPASS_ENABLED", "true")
    monkeypatch.setenv("MIMIR_FILE_SEARCH_AUTOPASS_K", "8")
    monkeypatch.setenv("MIMIR_FILE_SEARCH_AUTOPASS_MIN_CHARS", "30")
    cfg = Config.from_env()
    assert cfg.file_search_autopass_enabled is True
    assert cfg.file_search_autopass_k == 8
    assert cfg.file_search_autopass_min_chars == 30


# ---- end-to-end through Agent._build_turn_prompt ------------------------


@pytest.mark.asyncio
async def test_build_turn_prompt_includes_file_block_when_enabled(
    tmp_path: Path,
):
    """End-to-end: with the flag on, a user_message of sufficient
    length surfaces a ``Possibly relevant files`` section in the
    turn prompt."""
    _seed_indexable(tmp_path)
    indexer = Indexer(tmp_path, embedder=HashEmbedder())
    await indexer.start(run_initial_sweep=True, sweep_loop=False)
    try:
        agent = await _build_agent(tmp_path, indexer=indexer)
        event = AgentEvent(
            trigger="user_message",
            channel_id="discord-1",
            content="tell me about quantum entanglement and particle physics",
            author="discord-99",
        )
        ctx = _ctx()
        file_block = await agent._run_file_search_autopass(ctx, event)
        assert file_block is not None  # sanity: seeded index returns hits
        turn_prompt, _ = await agent._build_turn_prompt(
            ctx, event, saga_block=None, subagent_block=None,
            file_block=file_block,
        )
        assert "## Possibly relevant files" in turn_prompt
    finally:
        await indexer.stop()


@pytest.mark.asyncio
async def test_build_turn_prompt_omits_file_block_for_short_message(
    tmp_path: Path,
):
    """Flag on but message under min_chars → autopass returns None
    and the prompt has no Possibly-relevant-files section."""
    _seed_indexable(tmp_path)
    indexer = Indexer(tmp_path, embedder=HashEmbedder())
    await indexer.start(run_initial_sweep=True, sweep_loop=False)
    try:
        agent = await _build_agent(tmp_path, indexer=indexer)
        event = AgentEvent(
            trigger="user_message",
            channel_id="discord-1",
            content="ty",  # below min_chars
            author="discord-99",
        )
        ctx = _ctx()
        file_block = await agent._run_file_search_autopass(ctx, event)
        assert file_block is None
        turn_prompt, _ = await agent._build_turn_prompt(
            ctx, event, saga_block=None, subagent_block=None,
            file_block=file_block,
        )
        assert "Possibly relevant files" not in turn_prompt
    finally:
        await indexer.stop()


@pytest.mark.asyncio
async def test_build_turn_prompt_omits_file_block_when_disabled(
    tmp_path: Path,
):
    """Flag OFF (default) → autopass returns None even on a long
    user_message with a seeded index. Load-bearing for Sub B's A/B
    harness: the OFF arm must produce identical prompts to pre-Sub A.
    """
    _seed_indexable(tmp_path)
    indexer = Indexer(tmp_path, embedder=HashEmbedder())
    await indexer.start(run_initial_sweep=True, sweep_loop=False)
    try:
        agent = await _build_agent(
            tmp_path, indexer=indexer, file_search_autopass_enabled=False,
        )
        event = AgentEvent(
            trigger="user_message",
            channel_id="discord-1",
            content="tell me about quantum entanglement and particle physics",
            author="discord-99",
        )
        ctx = _ctx()
        file_block = await agent._run_file_search_autopass(ctx, event)
        assert file_block is None
        turn_prompt, _ = await agent._build_turn_prompt(
            ctx, event, saga_block=None, subagent_block=None,
            file_block=file_block,
        )
        assert "Possibly relevant files" not in turn_prompt
    finally:
        await indexer.stop()
