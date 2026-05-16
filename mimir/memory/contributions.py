"""Credit-pass for retrieved atoms.

After the agent generates a response from retrieved atoms, we want to
identify which atoms *actually contributed* to the answer — the
agent's implicit "this was useful" signal. Those atoms get a
``feedback_positive`` access event (weight 2.0), which:

- amplifies their activation on future retrievals
- populates ``observations_metadata.evidence_count`` (when the atom
  is an observation)
- gives the bench harness a per-question contribution_rate metric

The heuristic is phrase-overlap, not semantic similarity. Reasoning:

- An atom that contributed to the answer will share specific phrases
  with the response — direct quotes, named entities, dates, numbers.
- Cosine similarity over embeddings is too lenient: many atoms in
  the haystack will be embedding-near the response without having
  contributed.
- Saga's implementation does the same — explicit token / phrase
  overlap, not embedding similarity.

API: ``mark_contributions(retrieved_atoms, response_text)`` →
fires events on contributing atoms + returns counts. Called from
``MemoryClient.mark_contributions`` after the response is generated.

Bench: saga's bench has ``enable_mark_contributions = false`` because
per-question DB isolation means the contribution signal can't carry
across questions. mimir.memory keeps the same default — calling
``MemoryClient.mark_contributions`` is opt-in.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass

from .mark_access import AccessEvent, mark_access


logger = logging.getLogger("mimir.memory.contributions")


# Below this share of atom-phrases reappearing in the response, we
# don't credit the atom. 0.25 is saga's bench-tuned default — looser
# than seems intuitive because LongMemEval-style probes paraphrase
# heavily; a strict threshold under-credits the actual contributors.
DEFAULT_CONTRIBUTION_THRESHOLD = 0.25

# n-grams used to compute phrase overlap. 3-grams catch named entities
# (e.g. "Business Administration") and short phrases without losing too
# much signal to common-word noise. 2-grams are too noisy; 4-grams
# under-fire on shorter atoms.
NGRAM_N = 3

# Common closed-class words excluded from the n-gram pool. Without
# this every atom would share "of the", "and the", etc. with every
# response and the proportion would be useless. Same shape as the
# stopword set in fts.py but smaller — phrase overlap cares less
# about the long tail than BM25 does.
_PHRASE_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "is", "are", "was", "were",
    "be", "to", "of", "in", "on", "at", "by", "for", "with",
    "this", "that", "these", "those", "it", "its", "i", "you",
    "my", "your", "we", "our", "they", "their",
})


@dataclass
class ContributionResult:
    """Summary of which retrieved atoms got credit + the aggregate rate."""
    contributed_atom_ids: list[str]
    contribution_rate: float          # fraction of retrieved atoms above threshold
    scores: dict[str, float]          # atom_id → contribution score [0, 1]
    threshold: float


def mark_contributions(
    conn: sqlite3.Connection,
    retrieved_atoms: list[dict],
    response_text: str,
    *,
    session_id: str | None = None,
    threshold: float = DEFAULT_CONTRIBUTION_THRESHOLD,
    write_events: bool = True,
) -> ContributionResult:
    """Score each retrieved atom by phrase overlap with the response.

    Args:
        retrieved_atoms: the atoms that were returned to the model.
            Each dict must have ``id`` and ``content``. Other fields
            ignored.
        response_text: the model's output.
        session_id: optional session attribution for the access events.
        threshold: minimum atom-phrase-share to count as contributing.
        write_events: when True (default), fire ``feedback_positive``
            access events on contributors. Pass False to score-only
            (useful for the bench harness logging).

    Returns:
        ContributionResult with the contributor list, aggregate rate,
        per-atom scores, and the threshold used.

    Empty inputs are safe — returns an empty result without raising.
    """
    if not retrieved_atoms or not response_text or not response_text.strip():
        return ContributionResult(
            contributed_atom_ids=[],
            contribution_rate=0.0,
            scores={},
            threshold=threshold,
        )

    response_ngrams = _ngrams_of(response_text, NGRAM_N)
    if not response_ngrams:
        return ContributionResult(
            contributed_atom_ids=[],
            contribution_rate=0.0,
            scores={},
            threshold=threshold,
        )

    scores: dict[str, float] = {}
    contributors: list[str] = []
    for atom in retrieved_atoms:
        aid = atom.get("id")
        content = atom.get("content") or ""
        if not aid or not content:
            continue
        atom_ngrams = _ngrams_of(content, NGRAM_N)
        if not atom_ngrams:
            scores[aid] = 0.0
            continue
        # Proportion of the atom's distinct n-grams that appear in
        # the response. Anchoring on atom-side denom (not response-side)
        # rewards short atoms that contributed entirely without
        # penalizing long atoms that contributed only the relevant part.
        overlap = atom_ngrams & response_ngrams
        score = len(overlap) / len(atom_ngrams)
        scores[aid] = score
        if score >= threshold:
            contributors.append(aid)

    rate = len(contributors) / len(retrieved_atoms)

    if write_events and contributors:
        events = [
            AccessEvent(
                atom_id=aid,
                source="feedback_positive",
                session_id=session_id,
                metadata={
                    "trigger": "mark_contributions",
                    "score": round(scores[aid], 3),
                },
            )
            for aid in contributors
        ]
        try:
            conn.execute("BEGIN IMMEDIATE")
            mark_access(conn, events)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            logger.warning("mark_contributions event write failed: %s", exc)

    return ContributionResult(
        contributed_atom_ids=contributors,
        contribution_rate=rate,
        scores=scores,
        threshold=threshold,
    )


# ─── n-gram helpers ──────────────────────────────────────────────────


_TOKENIZE = re.compile(r"[A-Za-z0-9]+")


def _ngrams_of(text: str, n: int) -> set[tuple[str, ...]]:
    """Lowercased word tokens → set of n-grams (tuples). Stopwords
    excluded from individual positions but kept inside the n-gram
    if they border a content word — e.g. "lives in Boston" yields
    ("lives", "in", "boston") rather than skipping "in".

    Implementation note: keeping stopwords inside the tuple preserves
    the contextual meaning of named-entity phrases like "city of
    Boston" while still filtering pure-stopword n-grams like
    ("the", "of", "a"). Saga's heuristic does the same.
    """
    tokens = [t.lower() for t in _TOKENIZE.findall(text)]
    if len(tokens) < n:
        return set()
    out: set[tuple[str, ...]] = set()
    for i in range(len(tokens) - n + 1):
        gram = tuple(tokens[i : i + n])
        # Skip n-grams that are 100% stopwords.
        if all(t in _PHRASE_STOPWORDS for t in gram):
            continue
        out.add(gram)
    return out


__all__ = [
    "mark_contributions",
    "ContributionResult",
    "DEFAULT_CONTRIBUTION_THRESHOLD",
]
