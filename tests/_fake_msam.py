"""In-memory fake MSAM client for tests. Records calls so tests can assert
on the wire payloads without a network round-trip.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from mimir.msam_client import MsamError


@dataclass
class _Call:
    method: str
    payload: dict[str, Any]


@dataclass
class FakeMsam:
    """Behaves like ``MsamClient`` enough for the agent + hooks + tools."""

    calls: list[_Call] = field(default_factory=list)
    query_response: dict[str, Any] = field(default_factory=dict)
    end_session_atom_id: str = "atom-boundary-1"
    fail_on: set[str] = field(default_factory=set)

    async def health(self) -> bool:
        return "health" not in self.fail_on

    async def query(
        self,
        query: str,
        *,
        top_k: int = 12,
        mode: str = "task",
        token_budget: int = 500,
        session_id: str | None = None,
        min_confidence_tier: str | None = None,
        context: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            _Call("query", {"query": query, "top_k": top_k, "session_id": session_id,
                            "min_confidence_tier": min_confidence_tier,
                            "context": context})
        )
        if "query" in self.fail_on:
            raise MsamError("synthetic query failure")
        return self.query_response

    async def store(self, content: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(_Call("store", {"content": content, **kwargs}))
        return {"atom_id": "atom-stored"}

    async def feedback(
        self,
        atom_ids: list[str],
        response_text: str,
        *,
        session_id: str | None = None,
        feedback: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            _Call(
                "feedback",
                {
                    "atom_ids": list(atom_ids),
                    "response_text": response_text,
                    "session_id": session_id,
                    "feedback": feedback,
                },
            )
        )
        if "feedback" in self.fail_on:
            raise MsamError("synthetic feedback failure")
        return {"ok": True}

    async def outcome(
        self,
        atom_ids: list[str],
        feedback: str,
        *,
        session_id: str | None = None,
        query: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            _Call(
                "outcome",
                {
                    "atom_ids": list(atom_ids),
                    "feedback": feedback,
                    "session_id": session_id,
                    "query": query,
                },
            )
        )
        return {"ok": True}

    async def end_session(
        self,
        session_id: str,
        summary: str,
        *,
        topics_discussed: list[str] | None = None,
        decisions_made: list[str] | None = None,
        unfinished: list[str] | None = None,
        emotional_state: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            _Call(
                "end_session",
                {
                    "session_id": session_id,
                    "summary": summary,
                    "topics_discussed": topics_discussed,
                    "decisions_made": decisions_made,
                    "unfinished": unfinished,
                    "emotional_state": emotional_state,
                },
            )
        )
        return {"atom_id": self.end_session_atom_id, "session_id": session_id}

    async def consolidate(self, *, dry_run: bool = False, max_clusters: int | None = None) -> dict[str, Any]:
        self.calls.append(
            _Call("consolidate", {"dry_run": dry_run, "max_clusters": max_clusters})
        )
        return {"clusters_processed": 5, "atoms_merged": 12}

    recent_boundaries: list[dict[str, Any]] = field(default_factory=list)
    most_retrieved: list[dict[str, Any]] = field(default_factory=list)

    async def recent_session_boundaries(
        self, *, channel_id: str | None = None, count: int = 3,
    ) -> list[dict[str, Any]]:
        self.calls.append(
            _Call("recent_session_boundaries", {"channel_id": channel_id, "count": count})
        )
        if "recent_session_boundaries" in self.fail_on:
            return []
        out = list(self.recent_boundaries)
        if channel_id is not None:
            out = [b for b in out if b.get("channel_id") == channel_id]
        return out[:count]

    async def most_retrieved_atoms(
        self,
        *,
        days: int = 7,
        count: int = 10,
        channel_id: str | None = None,
        contributed_only: bool = False,
    ) -> list[dict[str, Any]]:
        self.calls.append(
            _Call(
                "most_retrieved_atoms",
                {
                    "days": days, "count": count, "channel_id": channel_id,
                    "contributed_only": contributed_only,
                },
            )
        )
        return list(self.most_retrieved)[:count]

    async def close(self) -> None:
        self.calls.append(_Call("close", {}))

    # ---- assertions ----

    def methods(self) -> list[str]:
        return [c.method for c in self.calls]

    def last(self, method: str) -> dict[str, Any] | None:
        for c in reversed(self.calls):
            if c.method == method:
                return c.payload
        return None
