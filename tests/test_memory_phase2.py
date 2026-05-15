"""Tests for Phase 2 features: mark_contributions, session-near
dedup, contextual query rewrite. All three are off-by-default — the
tests exercise opt-in surface and verify the off-path is a clean
no-op."""
from __future__ import annotations

import sqlite3
import struct
from hashlib import sha256
from pathlib import Path

import pytest


SCHEMA_PATH = Path(__file__).resolve().parent.parent / "mimir" / "memory" / "schema.sql"


# ─── shared fixtures ────────────────────────────────────────────────


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript(SCHEMA_PATH.read_text())
    return c


def _seed_atom(conn, atom_id: str, content: str, session_id: str | None = None):
    h = sha256(content.encode()).hexdigest()[:32]
    conn.execute(
        "INSERT INTO atoms (id, content, content_hash, created_at, session_id) "
        "VALUES (?, ?, ?, '2026-05-13T00:00:00Z', ?)",
        (atom_id, content, h, session_id),
    )
    conn.commit()


def _stub_provider(monkeypatch, dim_vec):
    """Stub the embedding provider to return a deterministic vector."""
    class _StubProvider:
        def embed(self, text, *, input_type="passage"):
            return list(dim_vec(text))
        def dimensions(self):
            return len(dim_vec(""))
    monkeypatch.setattr("mimir.memory.embeddings.get_provider", lambda: _StubProvider())
    def fake_get_config():
        def cfg(section, key, default=None):
            return {
                ("embedding", "max_input_chars"): 2000,
                ("embedding", "provider"): "stub",
                ("embedding", "model"): "stub",
            }.get((section, key), default)
        return cfg
    monkeypatch.setattr("mimir.memory._config_io.get_config", fake_get_config)


# ─── mark_contributions ──────────────────────────────────────────────


def test_mark_contributions_credits_overlapping_atoms(conn):
    from mimir.memory.contributions import mark_contributions
    _seed_atom(conn, "a1", "Alice graduated with a degree in Business Administration")
    _seed_atom(conn, "a2", "Bob enjoys verbose explanations of physics")
    response = (
        "Based on the chat history, the user graduated with a "
        "degree in Business Administration."
    )
    result = mark_contributions(
        conn,
        retrieved_atoms=[
            {"id": "a1", "content": "Alice graduated with a degree in Business Administration"},
            {"id": "a2", "content": "Bob enjoys verbose explanations of physics"},
        ],
        response_text=response,
    )
    assert "a1" in result.contributed_atom_ids
    assert "a2" not in result.contributed_atom_ids
    assert result.contribution_rate == 0.5


def test_mark_contributions_writes_feedback_positive_events(conn):
    from mimir.memory.contributions import mark_contributions
    _seed_atom(conn, "a1", "the daily commute takes 45 minutes each way")
    response = "The commute takes 45 minutes each way."
    mark_contributions(
        conn,
        retrieved_atoms=[
            {"id": "a1", "content": "the daily commute takes 45 minutes each way"},
        ],
        response_text=response,
    )
    rows = conn.execute(
        "SELECT source, weight FROM access_events "
        "WHERE atom_id = 'a1' AND source = 'feedback_positive'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][1] == 2.0  # SOURCE_WEIGHTS["feedback_positive"]


def test_mark_contributions_write_events_false_is_score_only(conn):
    from mimir.memory.contributions import mark_contributions
    _seed_atom(conn, "a1", "Alice has a degree in Business Administration")
    response = "She has a degree in Business Administration."
    result = mark_contributions(
        conn,
        retrieved_atoms=[
            {"id": "a1", "content": "Alice has a degree in Business Administration"},
        ],
        response_text=response,
        write_events=False,
    )
    assert "a1" in result.contributed_atom_ids
    rows = conn.execute(
        "SELECT 1 FROM access_events WHERE atom_id = 'a1' "
        "AND source = 'feedback_positive'"
    ).fetchall()
    assert rows == []


def test_mark_contributions_handles_empty_inputs(conn):
    from mimir.memory.contributions import mark_contributions
    r1 = mark_contributions(conn, [], "anything")
    r2 = mark_contributions(conn, [{"id": "x", "content": "y"}], "")
    assert r1.contribution_rate == 0.0
    assert r2.contribution_rate == 0.0


def test_mark_contributions_filters_stopword_only_ngrams(conn):
    """If two atoms share only stopword n-grams ('and the of'), they
    shouldn't credit each other. Heuristic guard."""
    from mimir.memory.contributions import mark_contributions
    _seed_atom(conn, "a1", "and the of and the of and the of")
    result = mark_contributions(
        conn,
        retrieved_atoms=[{"id": "a1", "content": "and the of and the of and the of"}],
        response_text="and the of and the of and the of",
        threshold=0.5,
    )
    # No content n-grams → no contribution.
    assert "a1" not in result.contributed_atom_ids


# ─── Session near-duplicate dedup ────────────────────────────────────


def test_session_dedup_off_by_default(conn):
    """Without a threshold, near-duplicate within the same session is
    stored as a separate atom (only content-hash dedupe applies)."""
    from mimir.memory.store import store

    def embed(text):
        # Two near-identical texts get vectors that differ slightly.
        if "fast" in text:
            return struct.pack("4f", 1.0, 0.0, 0.0, 0.1), "stub", "stub", 4
        return struct.pack("4f", 1.0, 0.0, 0.0, 0.0), "stub", "stub", 4

    r1 = store(conn, "Alice prefers concise replies",
               embed_fn=embed, session_id="s1")
    r2 = store(conn, "Alice prefers fast replies",
               embed_fn=embed, session_id="s1")
    assert r1.stored is True
    assert r2.stored is True
    assert r1.atom_id != r2.atom_id


def test_session_dedup_collapses_near_duplicate(conn):
    """With threshold set, a near-identical paraphrase in the SAME
    session dedupes to the prior atom."""
    from mimir.memory.store import store

    same_vec = struct.pack("4f", 1.0, 0.0, 0.0, 0.0)

    def embed(text):
        # Both texts return the same vector to make the cosine = 1.0.
        return same_vec, "stub", "stub", 4

    r1 = store(conn, "Alice prefers concise replies",
               embed_fn=embed, session_id="s1")
    r2 = store(conn, "Alice likes terse responses",  # different text → different content_hash
               embed_fn=embed, session_id="s1",
               session_dedup_threshold=0.95)
    assert r1.stored is True
    assert r2.stored is False
    assert r2.reason == "session_near_duplicate"
    assert r1.atom_id == r2.atom_id


def test_session_dedup_does_not_cross_sessions(conn):
    """Near-duplicate in a DIFFERENT session is stored separately —
    dedup is scoped to one session."""
    from mimir.memory.store import store

    same_vec = struct.pack("4f", 1.0, 0.0, 0.0, 0.0)

    def embed(text):
        return same_vec, "stub", "stub", 4

    r1 = store(conn, "Alice prefers concise replies",
               embed_fn=embed, session_id="s1")
    r2 = store(conn, "Alice likes terse responses",
               embed_fn=embed, session_id="s2",
               session_dedup_threshold=0.95)
    # Different sessions; no dedup.
    assert r1.stored is True
    assert r2.stored is True
    assert r1.atom_id != r2.atom_id


def test_session_dedup_threshold_floor_not_crossed(conn):
    """If best match is BELOW threshold, no dedup happens."""
    from mimir.memory.store import store

    def embed(text):
        if "concise" in text:
            return struct.pack("4f", 1.0, 0.0, 0.0, 0.0), "stub", "stub", 4
        # Orthogonal vector — cosine 0.0
        return struct.pack("4f", 0.0, 1.0, 0.0, 0.0), "stub", "stub", 4

    r1 = store(conn, "Alice prefers concise replies",
               embed_fn=embed, session_id="s1")
    r2 = store(conn, "Bob enjoys verbose explanations",
               embed_fn=embed, session_id="s1",
               session_dedup_threshold=0.95)
    # Threshold not crossed → new atom.
    assert r1.stored is True
    assert r2.stored is True


# ─── Contextual rewrite (off-path) ───────────────────────────────────


@pytest.mark.asyncio
async def test_rewrite_query_no_context_returns_original():
    from mimir.memory.query_rewrite import rewrite_query
    out = await rewrite_query("what about him?", context=None)
    assert out == "what about him?"


@pytest.mark.asyncio
async def test_rewrite_query_empty_context_returns_original():
    from mimir.memory.query_rewrite import rewrite_query
    out = await rewrite_query("what about him?", context=[])
    assert out == "what about him?"


@pytest.mark.asyncio
async def test_rewrite_query_uses_llm_when_context_present(monkeypatch):
    """With non-empty context, rewrite_query calls saga's call_llm
    and returns the cleaned LLM output."""
    from mimir.memory import query_rewrite

    async def fake_call_llm(cfg, *, prompt, max_tokens, temperature, system):
        # Simulate a clean rewrite.
        assert "Alice" in prompt
        return "What about Alice?\n"

    monkeypatch.setattr("mimir.memory._llm.call_llm", fake_call_llm)
    monkeypatch.setattr(
        "mimir.memory._config_io.resolve_llm_config", lambda subsystem: {"provider": "stub"},
    )

    out = await query_rewrite.rewrite_query(
        "what about him?",
        context=[
            {"role": "user", "content": "I met Alice at the conference"},
            {"role": "assistant", "content": "That's great!"},
        ],
    )
    assert out == "What about Alice?"


@pytest.mark.asyncio
async def test_rewrite_query_llm_failure_falls_back(monkeypatch):
    """LLM error → return original query."""
    from mimir.memory import query_rewrite

    async def boom(*a, **k):
        raise RuntimeError("transport down")

    monkeypatch.setattr("mimir.memory._llm.call_llm", boom)
    monkeypatch.setattr(
        "mimir.memory._config_io.resolve_llm_config", lambda subsystem: {"provider": "stub"},
    )

    out = await query_rewrite.rewrite_query(
        "what about him?",
        context=[{"role": "user", "content": "I met Alice"}],
    )
    assert out == "what about him?"


@pytest.mark.asyncio
async def test_rewrite_query_strips_preface_noise(monkeypatch):
    """LLM may echo 'Rewritten question:' or wrap in quotes."""
    from mimir.memory import query_rewrite

    async def fake_call_llm(*a, **k):
        return 'Rewritten question: "What did Alice say about Boston?"'

    monkeypatch.setattr("mimir.memory._llm.call_llm", fake_call_llm)
    monkeypatch.setattr(
        "mimir.memory._config_io.resolve_llm_config", lambda subsystem: {"provider": "stub"},
    )

    out = await query_rewrite.rewrite_query(
        "what did she say?",
        context=[{"role": "user", "content": "Alice mentioned Boston"}],
    )
    assert out == "What did Alice say about Boston?"


@pytest.mark.asyncio
async def test_rewrite_query_refusal_falls_back(monkeypatch):
    """If LLM says 'No change needed' / 'cannot rewrite', use original."""
    from mimir.memory import query_rewrite

    async def fake_call_llm(*a, **k):
        return "No change needed."

    monkeypatch.setattr("mimir.memory._llm.call_llm", fake_call_llm)
    monkeypatch.setattr(
        "mimir.memory._config_io.resolve_llm_config", lambda subsystem: {"provider": "stub"},
    )

    out = await query_rewrite.rewrite_query(
        "what about Alice?",
        context=[{"role": "user", "content": "We talked about Alice"}],
    )
    assert out == "what about Alice?"


@pytest.mark.asyncio
async def test_rewrite_query_strips_bare_rewritten_prefix(monkeypatch):
    """Updated 2026-05-13: the prompt now ends with literal ``Rewritten:``
    (matching saga), not ``Rewritten question:``. The LLM sometimes
    echoes the label verbatim — we strip either form."""
    from mimir.memory import query_rewrite

    async def fake_call_llm(*a, **k):
        return "Rewritten: look for my Sony headphones"

    monkeypatch.setattr("mimir.memory._llm.call_llm", fake_call_llm)
    monkeypatch.setattr(
        "mimir.memory._config_io.resolve_llm_config", lambda subsystem: {"provider": "stub"},
    )

    out = await query_rewrite.rewrite_query(
        "yes, look for that",
        context=[{"role": "user", "content": "I'm shopping for Sony headphones"}],
    )
    assert out == "look for my Sony headphones"


@pytest.mark.asyncio
async def test_rewrite_query_truncates_long_message_content(monkeypatch):
    """Each conversation turn's content is capped at 400 chars in the
    prompt. Long assistant explanations are the usual source of prompt
    bloat; pinning the per-turn cap keeps cost bounded as conversations
    grow. Matches saga's ``_resolve_contextual_query`` window."""
    from mimir.memory import query_rewrite

    captured_prompt: dict[str, str] = {}

    async def fake_call_llm(cfg, *, prompt, max_tokens, temperature, system):
        captured_prompt["text"] = prompt
        return "rewritten message"

    monkeypatch.setattr("mimir.memory._llm.call_llm", fake_call_llm)
    monkeypatch.setattr(
        "mimir.memory._config_io.resolve_llm_config", lambda subsystem: {"provider": "stub"},
    )

    long_msg = "x" * 5000  # well above the 400-char cap
    await query_rewrite.rewrite_query(
        "tell me more",
        context=[{"role": "assistant", "content": long_msg}],
    )
    # The full 5000-char string must NOT appear; the truncation marker
    # (ellipsis) signals the cap fired.
    assert "x" * 5000 not in captured_prompt["text"]
    assert "x" * 400 in captured_prompt["text"]  # cap kept first 400
    assert "…" in captured_prompt["text"]


@pytest.mark.asyncio
async def test_rewrite_query_caps_at_last_n_messages(monkeypatch):
    """Conversation windows longer than the cap drop the oldest turns.
    Saga uses last-10; we mirror that. Older context tends to be stale
    for reference resolution — the antecedent of a referential phrase
    is almost always within the recent window."""
    from mimir.memory import query_rewrite

    captured_prompt: dict[str, str] = {}

    async def fake_call_llm(cfg, *, prompt, max_tokens, temperature, system):
        captured_prompt["text"] = prompt
        return "rewritten message"

    monkeypatch.setattr("mimir.memory._llm.call_llm", fake_call_llm)
    monkeypatch.setattr(
        "mimir.memory._config_io.resolve_llm_config", lambda subsystem: {"provider": "stub"},
    )

    # 20 turns — the cap should drop the oldest 10. We tag each turn
    # with a unique substring so we can verify which made it in.
    history = [
        {"role": "user", "content": f"OLD_TURN_{i}"} for i in range(10)
    ] + [
        {"role": "user", "content": f"RECENT_TURN_{i}"} for i in range(10)
    ]
    await query_rewrite.rewrite_query("what about it?", context=history)
    # OLDest turns clipped.
    assert "OLD_TURN_0" not in captured_prompt["text"]
    assert "OLD_TURN_9" not in captured_prompt["text"]
    # Recent window survives.
    assert "RECENT_TURN_0" in captured_prompt["text"]
    assert "RECENT_TURN_9" in captured_prompt["text"]


def test_rewrite_prompt_carries_saga_rules():
    """Prompt structure pinning: when the LLM never runs (bench/prod
    default), no behavior to verify. Pin the prompt's load-bearing
    instructions instead — examples, intent-preservation, single-line
    output constraint. Matches saga's contextual-rewrite prompt so
    behavior at the model is comparable."""
    from mimir.memory.query_rewrite import REWRITE_PROMPT
    # Concrete reference-resolution examples (saga's three).
    assert "Sony headphones" in REWRITE_PROMPT
    # Intent-shape preservation rule — without this the LLM happily
    # rewrites statements as questions.
    assert "Do not turn statements into questions" in REWRITE_PROMPT
    # Single-line output discipline.
    assert "single line" in REWRITE_PROMPT
    # Broad framing — question, statement, or command (vs saga's
    # earlier question-only framing which we used to share).
    assert "question, a statement, or a command" in REWRITE_PROMPT
