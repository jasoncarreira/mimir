"""Conversation/session browser aggregation over turns, chat history, and SAGA.

This module builds the durable data shape consumed by the React session
browser. It intentionally stays read-only: records are synthesized from
``turns.jsonl``, ``messages/chat_history.jsonl``, and the SAGA ``sessions`` /
``atoms`` tables when available.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ._jsonl_tail import tail_jsonl_records

SESSION_GAP_SECONDS = 30 * 60


def _read_jsonl_tail(path: Path | None, *, max_records: int = 5000) -> list[dict[str, Any]]:
    if path is None or not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        for record in tail_jsonl_records(path):
            if isinstance(record, dict):
                out.append(record)
            if len(out) >= max_records:
                break
    except OSError:
        return []
    out.reverse()
    return out


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _iso(value: datetime | None, fallback: str = "") -> str:
    if value is None:
        return fallback
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if not isinstance(value, str):
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _preview(value: str, limit: int = 180) -> str:
    collapsed = " ".join(value.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3] + "..."


def _session_id_from_turn(turn: dict[str, Any]) -> str | None:
    for key in ("saga_session_id", "session_id"):
        value = turn.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    extra = turn.get("extra")
    if isinstance(extra, dict):
        value = extra.get("saga_session_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _turn_search_text(turn: dict[str, Any]) -> str:
    parts = [_text(turn.get("input")), _text(turn.get("output")), _text(turn.get("error"))]
    for item in _list(turn.get("injected_inputs")):
        if isinstance(item, dict):
            parts.append(_text(item.get("text")))
        elif isinstance(item, str):
            parts.append(item)
    return "\n".join(part for part in parts if part)


def _session_key_for_missing_id(turn: dict[str, Any], counters: dict[str, dict[str, Any]]) -> str:
    channel = _text(turn.get("channel_id")) or "unknown"
    parsed = _parse_ts(turn.get("ts"))
    state = counters.get(channel)
    if state is None:
        state = {"index": 1, "last": parsed}
        counters[channel] = state
    elif parsed is not None and state.get("last") is not None:
        gap = (parsed - state["last"]).total_seconds()
        if gap > SESSION_GAP_SECONDS:
            state["index"] += 1
    if parsed is not None:
        state["last"] = parsed
    return f"synthetic:{channel}:{state['index']}"


def _read_saga_sessions(saga_db: Path | None, *, limit: int = 500) -> dict[str, dict[str, Any]]:
    if saga_db is None or not saga_db.is_file():
        return {}
    try:
        conn = sqlite3.connect(f"file:{saga_db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return {}
    try:
        rows = conn.execute(
            """
            SELECT id, channel_id, started_at, ended_at, summary, reflected_at,
                   topics_discussed, decisions_made, unfinished, emotional_state,
                   closed_since
            FROM sessions
            ORDER BY COALESCE(reflected_at, ended_at, started_at) DESC
            LIMIT ?
            """,
            (max(1, min(limit, 1000)),),
        ).fetchall()
        return {
            str(row["id"]): {
                "session_id": row["id"],
                "channel_id": row["channel_id"],
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
                "summary": row["summary"] or "",
                "reflected_at": row["reflected_at"],
                "topics_discussed": _json_list(row["topics_discussed"]),
                "decisions_made": _json_list(row["decisions_made"]),
                "unfinished": _json_list(row["unfinished"]),
                "emotional_state": row["emotional_state"],
                "closed_since": _json_list(row["closed_since"]),
            }
            for row in rows
        }
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def _read_saga_atoms(saga_db: Path | None, session_ids: set[str], *, per_session: int = 8) -> dict[str, list[dict[str, Any]]]:
    if saga_db is None or not saga_db.is_file() or not session_ids:
        return {}
    try:
        conn = sqlite3.connect(f"file:{saga_db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return {}
    try:
        out: dict[str, list[dict[str, Any]]] = {}
        for sid in sorted(session_ids):
            rows = conn.execute(
                """
                SELECT id, content, memory_type, stream, source_type, topics, created_at
                FROM atoms
                WHERE tombstoned = 0 AND session_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (sid, max(1, min(per_session, 25))),
            ).fetchall()
            out[sid] = [
                {
                    "id": row["id"],
                    "content_preview": _preview(row["content"] or "", 220),
                    "memory_type": row["memory_type"],
                    "stream": row["stream"],
                    "source_type": row["source_type"],
                    "topics": _json_list(row["topics"]),
                    "created_at": row["created_at"],
                }
                for row in rows
            ]
        return out
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def build_sessions_payload(
    *,
    turns_log: Path,
    chat_history: Path | None = None,
    saga_db: Path | None = None,
    limit: int = 200,
    query: str = "",
    channel: str | None = None,
    trigger: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    turns = _read_jsonl_tail(turns_log)
    messages = _read_jsonl_tail(chat_history)
    saga_sessions = _read_saga_sessions(saga_db)

    sessions: dict[str, dict[str, Any]] = {}
    missing_counters: dict[str, dict[str, Any]] = {}

    def ensure(key: str, *, saga_id: str | None, channel_id: str | None) -> dict[str, Any]:
        session = sessions.get(key)
        if session is None:
            session = {
                "id": saga_id or key,
                "saga_session_id": saga_id,
                "channel_id": channel_id,
                "started_at": None,
                "ended_at": None,
                "last_activity_at": None,
                "turn_ids": [],
                "turns": [],
                "messages": [],
                "triggers": [],
                "summary": "",
                "unfinished": [],
                "related_saga_atoms": [],
                "search_text": "",
                "synthetic": saga_id is None,
            }
            sessions[key] = session
        if channel_id and not session.get("channel_id"):
            session["channel_id"] = channel_id
        return session

    for turn in turns:
        saga_id = _session_id_from_turn(turn)
        key = saga_id or _session_key_for_missing_id(turn, missing_counters)
        session = ensure(key, saga_id=saga_id, channel_id=_text(turn.get("channel_id")) or None)
        ts = _parse_ts(turn.get("ts"))
        if ts is not None:
            if session["started_at"] is None or ts < _parse_ts(session["started_at"]):
                session["started_at"] = _iso(ts)
            if session["last_activity_at"] is None or ts > _parse_ts(session["last_activity_at"]):
                session["last_activity_at"] = _iso(ts)
                session["ended_at"] = _iso(ts)
        turn_id = _text(turn.get("turn_id"))
        if turn_id:
            session["turn_ids"].append(turn_id)
        trig = _text(turn.get("trigger")) or "unknown"
        if trig not in session["triggers"]:
            session["triggers"].append(trig)
        session["turns"].append(
            {
                "turn_id": turn_id,
                "ts": _text(turn.get("ts")),
                "trigger": trig,
                "channel_id": _text(turn.get("channel_id")),
                "input_snippet": _preview(_text(turn.get("input"))),
                "output_snippet": _preview(_text(turn.get("error")) or _text(turn.get("output"))),
            }
        )
        session["search_text"] += "\n" + _turn_search_text(turn)

    for saga_id, boundary in saga_sessions.items():
        session = ensure(saga_id, saga_id=saga_id, channel_id=boundary.get("channel_id"))
        session["summary"] = boundary.get("summary") or session.get("summary") or ""
        session["unfinished"] = boundary.get("unfinished") or session.get("unfinished") or []
        session["topics_discussed"] = boundary.get("topics_discussed") or []
        session["decisions_made"] = boundary.get("decisions_made") or []
        session["closed_since"] = boundary.get("closed_since") or []
        for key_name in ("started_at", "ended_at", "reflected_at"):
            if boundary.get(key_name):
                session[key_name] = boundary[key_name]
        if not session.get("last_activity_at"):
            session["last_activity_at"] = boundary.get("reflected_at") or boundary.get("ended_at") or boundary.get("started_at")
        session["search_text"] += "\n" + "\n".join(
            [
                boundary.get("summary") or "",
                " ".join(str(x) for x in boundary.get("unfinished") or []),
                " ".join(str(x) for x in boundary.get("topics_discussed") or []),
            ]
        )

    for msg in messages:
        msg_channel = _text(msg.get("channel_id")) or None
        msg_ts = _parse_ts(msg.get("ts"))
        candidates = [
            s for s in sessions.values()
            if s.get("channel_id") == msg_channel
            and msg_ts is not None
            and _parse_ts(s.get("started_at")) is not None
            and _parse_ts(s.get("last_activity_at")) is not None
            and _parse_ts(s.get("started_at")) <= msg_ts <= _parse_ts(s.get("last_activity_at"))
        ]
        if not candidates:
            continue
        session = max(candidates, key=lambda s: _parse_ts(s.get("last_activity_at")) or datetime.min.replace(tzinfo=timezone.utc))
        session["messages"].append(
            {
                "ts": _text(msg.get("ts")),
                "kind": _text(msg.get("kind")) or "user_message",
                "author": msg.get("author_display") or msg.get("author"),
                "content": _text(msg.get("content")),
                "content_snippet": _preview(_text(msg.get("content"))),
                "msg_id": msg.get("msg_id"),
            }
        )
        session["search_text"] += "\n" + _text(msg.get("content"))

    atoms_by_session = _read_saga_atoms(saga_db, {saga_id for saga_id in saga_sessions})
    for saga_id, atoms in atoms_by_session.items():
        if saga_id in sessions:
            sessions[saga_id]["related_saga_atoms"] = atoms

    q = query.strip().lower()
    from_dt = _parse_ts(f"{date_from}T00:00:00Z" if date_from and len(date_from) == 10 else date_from) if date_from else None
    to_dt = _parse_ts(f"{date_to}T23:59:59Z" if date_to and len(date_to) == 10 else date_to) if date_to else None
    out = []
    for session in sessions.values():
        last = _parse_ts(session.get("last_activity_at"))
        if channel and session.get("channel_id") != channel:
            continue
        if trigger and trigger not in session.get("triggers", []):
            continue
        if from_dt is not None and last is not None and last < from_dt:
            continue
        if to_dt is not None and last is not None and last > to_dt:
            continue
        if q and q not in str(session.get("search_text") or "").lower():
            continue
        session = dict(session)
        session.pop("search_text", None)
        session["message_count"] = len(session["messages"])
        session["turn_count"] = len(session["turn_ids"])
        out.append(session)

    out.sort(key=lambda item: _parse_ts(item.get("last_activity_at")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    limited = out[: max(1, min(limit, 500))]
    channels = sorted({str(s.get("channel_id")) for s in sessions.values() if s.get("channel_id")})
    triggers = sorted({str(t) for s in sessions.values() for t in s.get("triggers", []) if t})
    return {
        "sessions": limited,
        "channels": channels,
        "triggers": triggers,
        "total": len(out),
        "limit": limit,
    }
