"""Turn LongMemEval haystack_sessions into MSAM atoms (one atom per turn)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Iterable


def _parse_session_date(raw: str) -> str:
    """Parse '2023/05/30 (Tue) 23:40' -> UTC ISO string for atom created_at."""
    clean = raw.split("(")[0].strip() + " " + raw.rsplit(" ", 1)[-1]
    for fmt in ("%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(clean, fmt).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    return datetime.now(timezone.utc).isoformat()


def _format_turn(date_iso: str, role: str, content: str) -> str:
    date_tag = date_iso[:10]
    return f"[{date_tag} {role}] {content.strip()}"


def iter_turns(haystack_sessions, haystack_dates, haystack_session_ids):
    for sid, sdate, turns in zip(haystack_session_ids, haystack_dates, haystack_sessions):
        iso = _parse_session_date(sdate)
        for i, turn in enumerate(turns):
            role = turn.get("role", "user")
            content = (turn.get("content") or "").strip()
            if not content:
                continue
            has_answer = bool(turn.get("has_answer"))
            yield {
                "session_id": sid,
                "session_date_iso": iso,
                "turn_index": i,
                "role": role,
                "content": content,
                "text_for_atom": _format_turn(iso, role, content),
                "has_answer": has_answer,
            }


def ingest_question(question: dict, batch_size: int = 256) -> dict:
    """
    Ingest every turn in a question's haystack as an atom.
    Embeds in batches for speed, backdates created_at to the session date,
    and stores session_id + turn metadata on the atom.
    Returns stats.
    """
    from msam.core import store_atom, get_db
    from msam.embeddings import get_provider

    turns = list(iter_turns(
        question["haystack_sessions"],
        question["haystack_dates"],
        question["haystack_session_ids"],
    ))
    if not turns:
        return {"ingested": 0, "skipped": 0, "total_turns": 0}

    provider = get_provider()
    texts = [t["text_for_atom"] for t in turns]

    embeddings: list = []
    for i in range(0, len(texts), batch_size):
        chunk = texts[i : i + batch_size]
        if hasattr(provider, "batch_embed"):
            embs = provider.batch_embed(chunk, input_type="passage")
        else:
            embs = [provider.embed(t, input_type="passage") for t in chunk]
        embeddings.extend(embs)

    assert len(embeddings) == len(turns), "embedding count mismatch"

    ingested = 0
    skipped = 0
    stored_ids: list[tuple[str, str]] = []  # (atom_id, session_date_iso)

    for turn, emb in zip(turns, embeddings):
        result = store_atom(
            content=turn["text_for_atom"],
            stream="episodic" if turn["role"] == "user" else "semantic",
            profile="standard",
            arousal=0.5,
            valence=0.0,
            topics=[],
            encoding_confidence=0.9,
            source_type="longmemeval",
            metadata={
                "session_id": turn["session_id"],
                "session_date": turn["session_date_iso"],
                "turn_index": turn["turn_index"],
                "role": turn["role"],
                "has_answer": turn["has_answer"],
            },
            embedding=emb,
            agent_id="longmemeval",
        )
        if isinstance(result, str):
            ingested += 1
            stored_ids.append((result, turn["session_date_iso"]))
        else:
            skipped += 1

    # Backdate created_at so temporal queries see the real session timeline.
    conn = get_db()
    for atom_id, iso in stored_ids:
        conn.execute(
            "UPDATE atoms SET created_at = ? WHERE id = ?",
            (iso, atom_id),
        )
    conn.commit()

    return {
        "ingested": ingested,
        "skipped": skipped,
        "total_turns": len(turns),
    }
