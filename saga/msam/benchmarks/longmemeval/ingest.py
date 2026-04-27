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

    P10: After all turns land, write one ``session_boundary`` episodic atom
    per haystack session so retrieval has session-level beacons and the
    prediction warmup gate has source material.
    Returns stats.
    """
    from msam.core import store_atom, store_session_boundary, get_db
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
    # P32: collect semantic atoms for batch triple extraction. Only the
    # semantic stream gets triples (matching /v1/store behavior). Episodic
    # atoms are user turns; their triples would be too noisy.
    semantic_for_triples: list[tuple[str, str]] = []  # (atom_id, content)

    for turn, emb in zip(turns, embeddings):
        stream = "episodic" if turn["role"] == "user" else "semantic"
        result = store_atom(
            content=turn["text_for_atom"],
            stream=stream,
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
            if stream == "semantic":
                semantic_for_triples.append((result, turn["text_for_atom"]))
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

    # P10: write one episodic session_boundary per haystack session so
    # multi-session probes can retrieve session-level beacons. Summary is
    # deliberately minimal to avoid polluting retrieval with synthetic content.
    # Gated because boundary atoms compete for retrieval slots and may
    # displace genuine raw atoms in the top-K.
    boundary_stored = 0
    from msam.config import get_config as _gc
    if _gc()('benchmark', 'enable_session_boundaries', True):
        session_groups: dict[str, list[dict]] = {}
        for turn in turns:
            session_groups.setdefault(turn["session_id"], []).append(turn)

        boundary_ids: list[tuple[str, str]] = []
        for sid, sturns in session_groups.items():
            n_user = sum(1 for t in sturns if t["role"] == "user")
            n_asst = len(sturns) - n_user
            date_iso = sturns[0]["session_date_iso"]
            date_tag = date_iso[:10]
            summary = f"{date_tag}: conversation with {n_user} user turns, {n_asst} assistant turns"
            try:
                bid = store_session_boundary(session_id=sid, summary=summary)
                if isinstance(bid, str):
                    boundary_stored += 1
                    boundary_ids.append((bid, date_iso))
            except Exception:
                pass

        if boundary_ids:
            for atom_id, iso in boundary_ids:
                conn.execute(
                    "UPDATE atoms SET created_at = ? WHERE id = ?",
                    (iso, atom_id),
                )
            conn.commit()

    # P32: batch-extract triples for the semantic atoms now that they're
    # all stored. Gated by [triples] enable_extraction. Uses P7's batched
    # LLM call so the per-question cost is ~ceil(n_semantic/20) calls
    # instead of n_semantic.
    triples_extracted = 0
    if _gc()('triples', 'enable_extraction', False) and semantic_for_triples:
        try:
            from msam.triples import batch_extract_and_store
            triples_extracted = batch_extract_and_store(semantic_for_triples)
        except Exception:
            triples_extracted = 0

    return {
        "ingested": ingested,
        "skipped": skipped,
        "total_turns": len(turns),
        "session_boundaries": boundary_stored,
        "triples_extracted": triples_extracted,
    }
