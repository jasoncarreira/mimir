"""
SAGA Consolidation Engine -- Sleep-inspired memory consolidation.

Clusters similar atoms, synthesizes abstractions via LLM, and reduces
source atom stability. This is how human memory works during sleep:
50 similar atoms become 1 abstraction.

Also a critical scaling strategy -- keeps the active atom count bounded
regardless of ingestion rate.

Usage:
    from saga.consolidation import ConsolidationEngine
    engine = ConsolidationEngine()
    result = engine.consolidate(dry_run=True)
"""

import json
import logging
import hashlib
from datetime import datetime, timezone

from .config import get_config
from .core import get_db, store_atom, embed_text, pack_embedding, unpack_embedding

logger = logging.getLogger("saga.consolidation")

_cfg = get_config()

# ─── Configuration ──────────────────────────────────────────────

DEFAULT_SIMILARITY_THRESHOLD = _cfg('consolidation', 'similarity_threshold', 0.80)
DEFAULT_MIN_CLUSTER_SIZE = _cfg('consolidation', 'min_cluster_size', 3)
DEFAULT_MAX_CLUSTERS = _cfg('consolidation', 'max_clusters_per_run', 50)
DEFAULT_STABILITY_REDUCTION = _cfg('consolidation', 'stability_reduction_factor', 0.5)


# ─── P35: structured-output parsing for consolidation ───────────

# P48: canonical-vocabulary seeding for the consolidation prompt.
# The LLM gets nudged (not forced) to reuse these intent predicates
# instead of inventing compound domain-specific ones (prefers vs
# prefers_podcast_length). Detail moves into the OBJECT.
_CANONICAL_PREDICATE_SEED: list[str] = [
    # Personal-claim intents (the long tail offender pre-P48).
    "prefers", "likes", "dislikes", "loves", "hates",
    "has", "owns", "uses",
    "works_at", "lives_in", "lived_in",
    "knows", "discussed", "mentioned", "asked_about",
    "recommends", "follows",
    # Domain-action verbs commonly seen organically (kept as
    # canonical so the seed list looks realistic to the LLM).
    "offers", "includes", "provides", "supports",
    "located_in", "has_feature",
]
_CANONICAL_SUBJECT_SEED: list[str] = ["User"]


def _canonical_vocab_block(
    conn,
    *,
    top_n_predicates: int = 25,
    top_n_subjects: int = 15,
    extra_subjects: list[str] | None = None,
) -> str:
    """Build the canonical-predicate / canonical-subject context block
    for the consolidation prompt. Surfaces existing high-frequency
    predicates and subjects from the DB so the LLM can canonicalize
    against the live vocabulary instead of inventing fresh predicate
    names per cluster (the 9,997-distinct-predicate problem from the
    P42 Sonnet bench corpus).

    Falls back to a static seed when the DB has no triples yet
    (cold start). The static seed is also unioned with whatever the
    DB returns, so a fresh DB sees the seed and starts canonicalizing
    immediately rather than re-deriving the canonical set from zero.

    ``extra_subjects`` (optional) — operator-supplied canonical names
    that should always appear in the subjects list regardless of DB
    contents. mimir uses this to inject identities.yaml's canonical
    forms ("Tim", "Alice", etc.) so the consolidation LLM uses the
    operator-curated names instead of whatever surface form the
    source atoms happen to mention. Rendered without counts, like
    seed entries — they're authoritative-by-fiat, not frequency-
    derived.

    Returns an empty string when the helper can't read the DB —
    consolidation continues with the bare prompt rather than crashing.
    """
    pred_lines: list[str] = []
    subj_lines: list[str] = []
    seen_preds: set[str] = set()
    seen_subjs: set[str] = set()
    try:
        rows = conn.execute(
            "SELECT predicate, COUNT(*) c FROM triples "
            "WHERE state = 'active' "
            "GROUP BY predicate ORDER BY c DESC LIMIT ?",
            (top_n_predicates,),
        ).fetchall()
        for r in rows:
            pred = (r[0] if not isinstance(r, dict) else r["predicate"]) or ""
            cnt = (r[1] if not isinstance(r, dict) else r["c"]) or 0
            if pred and pred not in seen_preds:
                pred_lines.append(f"{pred} ({int(cnt)})")
                seen_preds.add(pred)
        rows = conn.execute(
            "SELECT subject, COUNT(*) c FROM triples "
            "WHERE state = 'active' "
            "GROUP BY subject ORDER BY c DESC LIMIT ?",
            (top_n_subjects,),
        ).fetchall()
        for r in rows:
            subj = (r[0] if not isinstance(r, dict) else r["subject"]) or ""
            cnt = (r[1] if not isinstance(r, dict) else r["c"]) or 0
            if subj and subj not in seen_subjs:
                subj_lines.append(f"{subj} ({int(cnt)})")
                seen_subjs.add(subj)
    except Exception:
        # DB read failed — fall back to seed-only.
        pass

    # Union with the static seed so a fresh DB still sees the canonical
    # vocabulary. Seed entries omit counts to keep them visually distinct
    # from real DB-derived counts.
    for p in _CANONICAL_PREDICATE_SEED:
        if p not in seen_preds:
            pred_lines.append(p)
            seen_preds.add(p)
    for s in _CANONICAL_SUBJECT_SEED:
        if s not in seen_subjs:
            subj_lines.append(s)
            seen_subjs.add(s)
    # Operator-supplied canonical subjects (mimir's identities.yaml
    # entries, typically). Rendered without counts — same shape as
    # the static seed; authoritative-by-fiat.
    if extra_subjects:
        for s in extra_subjects:
            if isinstance(s, str) and s.strip() and s not in seen_subjs:
                subj_lines.append(s.strip())
                seen_subjs.add(s.strip())

    if not pred_lines and not subj_lines:
        return ""

    parts: list[str] = []
    parts.append(
        "Existing canonical vocabulary (PREFER reusing these — counts "
        "in parens for DB-derived entries; bare names are seed values):"
    )
    if pred_lines:
        parts.append("Predicates: " + ", ".join(pred_lines))
    if subj_lines:
        parts.append("Subjects: " + ", ".join(subj_lines))
    parts.append("")  # trailing newline before the next prompt section
    return "\n".join(parts) + "\n"


def _parse_structured_synthesis(
    text: str,
) -> tuple[str | None, list[dict], list[str]]:
    """Parse the OBSERVATION + TRIPLES + CONTRADICTIONS triple-output
    format.

    Returns ``(observation_text_or_None, list_of_triple_dicts,
    list_of_contradiction_lines)``. On parse failure returns
    ``(None, [], [])``. If only OBSERVATION parses cleanly, returns
    ``(observation, [], [])`` — graceful degradation when the LLM
    omits or malforms downstream sections.

    Triples are returned without atom_id; the caller fills it in once
    the observation is stored. Contradictions are raw single-line
    strings the LLM produced; consumer (P25 audit) does the further
    parsing into structured form."""
    import re
    if not text:
        return None, [], []

    # Find the OBSERVATION section header. Tolerant of leading
    # whitespace and either `OBSERVATION:` or `**OBSERVATION:**`.
    obs_match = re.search(r'(?im)^\s*\**\s*OBSERVATION\s*:?\s*\**\s*\n?', text)
    if not obs_match:
        # No header — assume the whole response is the observation
        # (legacy single-output format).
        return text.strip() or None, [], []

    after_obs = text[obs_match.end():]

    # Find the TRIPLES section, which terminates the observation.
    tri_match = re.search(r'(?im)^\s*\**\s*TRIPLES\s*:?\s*\**\s*\n?', after_obs)
    if tri_match:
        observation = after_obs[:tri_match.start()].strip()
        triples_block_full = after_obs[tri_match.end():].strip()
    else:
        observation = after_obs.strip()
        triples_block_full = ""

    if not observation:
        observation = None

    # Split TRIPLES vs CONTRADICTIONS section. Either may be absent;
    # CONTRADICTIONS, when present, terminates TRIPLES.
    contra_split = re.split(
        r'(?im)^\s*\**\s*CONTRADICTIONS\s*:?\s*\**\s*\n',
        triples_block_full,
        maxsplit=1,
    )
    triples_block = contra_split[0].strip()
    contradictions_block = contra_split[1].strip() if len(contra_split) > 1 else ""

    # NOTES/EXPLANATION can still trail either; trim them off both.
    triples_block = re.split(
        r'(?im)^\s*\**\s*(?:NOTES|EXPLANATION)\s*:?\s*\**\s*\n',
        triples_block,
    )[0]
    contradictions_block = re.split(
        r'(?im)^\s*\**\s*(?:NOTES|EXPLANATION)\s*:?\s*\**\s*\n',
        contradictions_block,
    )[0]

    triples: list[dict] = []
    if triples_block and "NONE" not in triples_block.upper().split("\n")[0]:
        # Same triple shape as triples._parse_triples — reuse its
        # validation. Pass empty atom_id; caller fills it in.
        from .triples import _parse_triples
        triples = _parse_triples(triples_block, atom_id="")

    contradictions: list[str] = []
    if contradictions_block and "NONE" not in contradictions_block.upper().split("\n")[0]:
        for line in contradictions_block.splitlines():
            line = line.strip("- *\t ")
            if line and not line.upper().startswith("NONE"):
                contradictions.append(line)

    return observation, triples, contradictions


class ConsolidationEngine:
    """Sleep-inspired memory consolidation.

    Clusters similar atoms, synthesizes abstractions via LLM,
    reduces source atom stability.
    """

    def __init__(self, similarity_threshold: float = None,
                 min_cluster_size: int = None,
                 max_clusters: int = None,
                 stability_reduction: float = None):
        self.similarity_threshold = similarity_threshold or DEFAULT_SIMILARITY_THRESHOLD
        self.min_cluster_size = min_cluster_size or DEFAULT_MIN_CLUSTER_SIZE
        self.max_clusters = max_clusters or DEFAULT_MAX_CLUSTERS
        self.stability_reduction = stability_reduction or DEFAULT_STABILITY_REDUCTION
        self._last_skipped_existing = 0

    # VSM: S3 (saga-internal) — sleep-inspired consolidation. Clusters
    #      similar atoms, LLM-synthesizes one observation per cluster,
    #      reduces source-atom stability. Output feeds the two-tier
    #      retrieval pathway and (P37) the world-model audit.
    # loop_id: 4.3
    async def consolidate(
        self,
        dry_run: bool = False,
        max_clusters: int = None,
        extra_canonical_subjects: list[str] | None = None,
    ) -> dict:
        """Main entry: cluster -> synthesize -> restructure.

        Args:
            dry_run: If True, detect clusters but don't synthesize or restructure.
            max_clusters: Override max clusters for this run.
            extra_canonical_subjects: P48 — operator-supplied canonical
                subject names (e.g. mimir's identities.yaml canonicals)
                to surface in the consolidation prompt's vocab block.
                None / empty = seed-only behavior.

        Returns:
            Dict with clusters_found, clusters_consolidated, atoms_affected, etc.
        """
        max_clusters = max_clusters or self.max_clusters
        self._extra_canonical_subjects = list(extra_canonical_subjects or [])

        # Phase 1: Cluster
        clusters = self._cluster_phase()
        clusters = clusters[:max_clusters]

        if dry_run:
            # Compute per-cluster average similarity for diagnostics
            from .core import cosine_similarity
            cluster_details = []
            total_sim = 0.0
            total_pairs = 0
            for c in clusters:
                pairs_sim = []
                vecs = [unpack_embedding(a['embedding']) for a in c if a.get('embedding')]
                for i in range(len(vecs)):
                    for j in range(i + 1, len(vecs)):
                        pairs_sim.append(cosine_similarity(vecs[i], vecs[j]))
                avg_sim = sum(pairs_sim) / len(pairs_sim) if pairs_sim else 0.0
                total_sim += sum(pairs_sim)
                total_pairs += len(pairs_sim)
                cluster_details.append({
                    "size": len(c),
                    "stream": c[0].get("stream", "semantic"),
                    "avg_similarity": round(avg_sim, 3),
                    "preview": [a["content"][:80] for a in c[:3]],
                })
            return {
                "dry_run": True,
                "clusters_found": len(clusters),
                "clusters": cluster_details,
                "total_atoms_in_clusters": sum(len(c) for c in clusters),
                "avg_similarity": round(total_sim / total_pairs, 3) if total_pairs else 0.0,
            }

        # Phase 2: Synthesize
        syntheses = await self._synthesize_phase(clusters)

        # Phase 3: Restructure
        result = self._restructure_phase(syntheses)

        result["clusters_found"] = len(clusters)
        result["clusters_consolidated"] = len(syntheses)
        result["clusters_skipped_existing"] = self._last_skipped_existing
        result["dry_run"] = False
        # Intent-named alias for callers that care about "how many
        # observation atoms did this run actually create?"
        # (== synthesis_atoms_stored from _restructure_phase under
        # normal conditions; differs only if a synthesis stored
        # successfully without the LLM, which is rare.)
        result["observations_created"] = result.get(
            "synthesis_atoms_stored", len(syntheses)
        )

        return result

    def _cluster_phase(self) -> list[list[dict]]:
        """Use FAISS (or brute-force) to find clusters of similar atoms.

        Filters:
        - Only active atoms with embeddings
        - Never cluster pinned atoms
        - memory_type='raw' only (observations are consolidation output, not input)
        - Same stream only
        - Minimum cluster size
        """
        conn = get_db()
        rows = conn.execute("""
            SELECT id, content, stream, embedding, access_count, topics, is_pinned
            FROM atoms
            WHERE state = 'active' AND embedding IS NOT NULL AND is_pinned = 0
              AND (memory_type IS NULL OR memory_type = 'raw')
        """).fetchall()

        if not rows:
            conn.close()
            return []

        atoms = [dict(r) for r in rows]
        atom_map = {a['id']: a for a in atoms}

        # Group by stream
        stream_groups = {}
        for atom in atoms:
            stream_groups.setdefault(atom['stream'], []).append(atom)

        clusters = []
        for stream, group in stream_groups.items():
            if len(group) < self.min_cluster_size:
                continue
            stream_clusters = self._find_clusters_in_group(group, conn)
            clusters.extend(stream_clusters)

        conn.close()

        # Sort by cluster size (largest first)
        clusters.sort(key=len, reverse=True)
        return clusters

    def _find_clusters_in_group(self, atoms: list[dict], conn) -> list[list[dict]]:
        """Find clusters within a stream group using FAISS k-NN or brute-force."""
        # Try FAISS
        try:
            from .vector_index import get_atoms_index, FAISS_AVAILABLE
            if FAISS_AVAILABLE:
                return self._cluster_with_faiss(atoms, conn)
        except Exception:
            pass

        # Fallback: simple greedy clustering
        return self._cluster_brute_force(atoms)

    def _cluster_with_faiss(self, atoms: list[dict], conn) -> list[list[dict]]:
        """Use FAISS to find clusters via k-NN graph."""
        from .vector_index import get_atoms_index

        idx = get_atoms_index(conn=conn)
        if idx is None or not idx._built:
            return self._cluster_brute_force(atoms)

        clustered = set()
        clusters = []

        for atom in atoms:
            if atom['id'] in clustered:
                continue

            vec = unpack_embedding(atom['embedding'])
            neighbors = idx.search(vec, top_k=20)

            cluster = [atom]
            clustered.add(atom['id'])

            atom_map = {a['id']: a for a in atoms}
            for neighbor_id, sim in neighbors:
                if neighbor_id == atom['id'] or neighbor_id in clustered:
                    continue
                if sim < self.similarity_threshold:
                    continue
                neighbor = atom_map.get(neighbor_id)
                if neighbor and neighbor['stream'] == atom['stream']:
                    cluster.append(neighbor)
                    clustered.add(neighbor_id)

            if len(cluster) >= self.min_cluster_size:
                clusters.append(cluster)

        return clusters

    def _cluster_brute_force(self, atoms: list[dict]) -> list[list[dict]]:
        """Simple greedy clustering using pairwise cosine similarity."""
        from .core import cosine_similarity

        clustered = set()
        clusters = []

        for i, atom in enumerate(atoms):
            if atom['id'] in clustered:
                continue

            cluster = [atom]
            clustered.add(atom['id'])
            vec_a = unpack_embedding(atom['embedding'])

            for j in range(i + 1, len(atoms)):
                other = atoms[j]
                if other['id'] in clustered:
                    continue
                vec_b = unpack_embedding(other['embedding'])
                sim = cosine_similarity(vec_a, vec_b)
                if sim >= self.similarity_threshold:
                    cluster.append(other)
                    clustered.add(other['id'])

            if len(cluster) >= self.min_cluster_size:
                clusters.append(cluster)

        return clusters

    def _candidate_observations_for_cluster(self, source_ids: list[str]) -> dict[str, set[str]]:
        """Fetch active/fading observations whose evidenced_by source set
        overlaps with ``source_ids``, returning {obs_id: existing_source_set}.

        Single shared query feeds both the identity check (skip on exact match)
        and the subset check (supersede on strict superset).
        """
        if not source_ids:
            return {}
        conn = get_db()
        try:
            placeholders = ",".join("?" * len(source_ids))
            rows = conn.execute(
                f"SELECT DISTINCT source_id FROM atom_relations "
                f"WHERE relation_type = 'evidenced_by' "
                f"AND target_id IN ({placeholders})",
                tuple(source_ids),
            ).fetchall()
            candidate_ids = [r[0] for r in rows]
            if not candidate_ids:
                return {}
            out: dict[str, set[str]] = {}
            for obs_id in candidate_ids:
                state_row = conn.execute(
                    "SELECT state FROM atoms WHERE id = ?", (obs_id,)
                ).fetchone()
                if not state_row or state_row[0] not in ("active", "fading"):
                    continue
                obs_rows = conn.execute(
                    "SELECT target_id FROM atom_relations "
                    "WHERE relation_type = 'evidenced_by' AND source_id = ?",
                    (obs_id,),
                ).fetchall()
                existing_set = {r[0] for r in obs_rows}
                if existing_set:
                    out[obs_id] = existing_set
            return out
        finally:
            conn.close()

    def _existing_observation_for_cluster(self, source_ids: list[str]) -> str | None:
        """Find an active/fading observation whose evidenced_by source set is
        identical to the given cluster source_ids. Used by the synthesize
        phase to skip re-running the LLM on a cluster that's already been
        consolidated.
        """
        target_set = set(source_ids or [])
        if not target_set:
            return None
        for obs_id, existing_set in self._candidate_observations_for_cluster(source_ids).items():
            if existing_set == target_set:
                return obs_id
        return None

    def _subset_observations_for_cluster(self, source_ids: list[str]) -> list[str]:
        """Find active/fading observations whose evidenced_by source set is a
        strict subset of the given cluster source_ids.

        Used by the synthesize phase to identify observations that are
        obsoleted by a new observation that covers strictly more evidence —
        those will receive a ``supersedes`` edge from the new observation in
        the restructure phase.
        """
        target_set = set(source_ids or [])
        if len(target_set) < 2:
            return []
        out = []
        for obs_id, existing_set in self._candidate_observations_for_cluster(source_ids).items():
            if existing_set and existing_set < target_set:  # strict subset
                out.append(obs_id)
        return out

    def _persist_consolidation_triples(
        self,
        cluster_triples: list[dict],
        new_obs_id: str,
        superseded_obs_ids: list[str],
    ) -> int:
        """Persist triples emitted by the consolidation LLM.

        For each emitted triple, three cases:

        1. **New triple** (no existing row with this content): INSERT
           a fresh row attached to ``new_obs_id``.
        2. **Restated triple** (same content as a row attached to one
           of the about-to-be-superseded observations): UPDATE the
           existing row's ``atom_id`` to ``new_obs_id``. Preserves
           dedup and follows the LLM's "still-true" verdict.
        3. **Pre-existing on a non-superseded observation** (rare —
           e.g., the same SPO is genuinely attested by two unrelated
           clusters): leave the existing row alone, no insert. Acts as
           the natural dedup layer.

        Returns the number of triples successfully persisted (counts
        both inserts and ownership transfers).
        """
        if not cluster_triples:
            return 0
        from .triples import _triple_text
        from .core import pack_embedding
        from .embeddings import batch_embed_texts
        import hashlib
        from datetime import datetime, timezone

        # Resolve atom_id and compute the content-level triple_id once
        # per emitted triple. Skip triples that fail validation (no
        # content fields).
        prepared: list[tuple[str, dict]] = []
        for t in cluster_triples:
            subj = (t.get("subject") or "").strip()
            pred = (t.get("predicate") or "").strip()
            obj = (t.get("object") or "").strip()
            if not (subj and pred and obj):
                continue
            norm_key = f"{subj.lower()}:{pred.lower()}:{obj.lower()}"
            tid = hashlib.sha256(norm_key.encode()).hexdigest()[:16]
            prepared.append((tid, {**t, "subject": subj, "predicate": pred, "object": obj}))

        if not prepared:
            return 0

        persisted = 0
        now = datetime.now(timezone.utc).isoformat()
        superseded_set = set(superseded_obs_ids or [])

        from .core import transactional

        # Embeddings live outside the transaction — they can be slow
        # network calls. The pre-existing-id probe also runs without
        # the write lock so we can size the embedding batch correctly.
        probe = get_db()
        try:
            placeholders = ",".join("?" * len(prepared))
            existing_rows = probe.execute(
                f"SELECT id FROM triples WHERE id IN ({placeholders})",
                tuple(tid for tid, _ in prepared),
            ).fetchall()
        finally:
            probe.close()
        existing_ids = {r[0] for r in existing_rows}

        new_triples = [(tid, t) for tid, t in prepared if tid not in existing_ids]
        embeddings_by_tid: dict[str, bytes | None] = {}
        if new_triples:
            texts = [
                _triple_text(t["subject"], t["predicate"], t["object"])
                for _, t in new_triples
            ]
            try:
                vecs = batch_embed_texts(texts)
            except Exception:
                vecs = [None] * len(texts)
            for (tid, _), vec in zip(new_triples, vecs):
                embeddings_by_tid[tid] = pack_embedding(vec) if vec else None

        # CR#16: per-triple INSERTs and ownership-transfer UPDATEs
        # ran in autocommit with try/except: pass around each write.
        # A real conflict (e.g., FK violation) silently dropped
        # individual triples while the rest of the cluster persisted —
        # the cluster's "we synthesized N triples" claim could be off
        # by half. Wrap the whole batch.
        with transactional() as conn:
            for tid, t in prepared:
                row = conn.execute(
                    "SELECT atom_id FROM triples WHERE id = ?", (tid,)
                ).fetchone()

                if row is None:
                    # Fresh insert — embedding pulled from the batch above.
                    embedding = embeddings_by_tid.get(tid)
                    # P37(a): pass through valid_from/valid_until from the
                    # consolidation LLM output. Both nullable; null means
                    # "always valid" per query_world's filter logic.
                    valid_from = t.get("valid_from")
                    valid_until = t.get("valid_until")
                    conn.execute(
                        "INSERT OR IGNORE INTO triples "
                        "(id, atom_id, subject, predicate, object, "
                        " confidence, embedding, created_at, "
                        " valid_from, valid_until) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (tid, new_obs_id, t["subject"], t["predicate"],
                         t["object"], float(t.get("confidence", 1.0)),
                         embedding, now, valid_from, valid_until),
                    )
                    persisted += 1
                    continue

                existing_atom_id = row[0]
                if existing_atom_id == new_obs_id:
                    # Already attached — defensive idempotency, no-op.
                    continue
                if existing_atom_id in superseded_set:
                    # Restate case: transfer ownership to the new
                    # (superseding) observation. The LLM saw this
                    # prior triple in context and chose to restate it,
                    # which means it survives the merger.
                    conn.execute(
                        "UPDATE triples SET atom_id = ?, created_at = ? WHERE id = ?",
                        (new_obs_id, now, tid),
                    )
                    persisted += 1
                # else: triple is attested by some other unrelated
                # observation. Leave it alone — content-level dedup is
                # the right default outside the supersedes window.

        return persisted

    def _fetch_prior_triples(self, observation_ids: list[str]) -> list[dict]:
        """Return active triples attached to the given observation atoms.

        Used when a new cluster is a strict superset of an existing
        observation — the synthesizer prompt includes these as
        "previous beliefs" context so the LLM can decide whether to
        keep, update, or drop each one in its emitted TRIPLES.

        Returns dicts with subject/predicate/object only — atom_id and
        embedding are intentionally omitted (this is just for prompt
        framing). Capped at 20 entries to keep prompts bounded.
        """
        if not observation_ids:
            return []
        conn = get_db()
        try:
            placeholders = ",".join("?" * len(observation_ids))
            rows = conn.execute(
                f"SELECT subject, predicate, object FROM triples "
                f"WHERE atom_id IN ({placeholders}) AND state = 'active' "
                f"LIMIT 20",
                tuple(observation_ids),
            ).fetchall()
        except Exception:
            rows = []
        finally:
            conn.close()
        return [
            {"subject": r[0], "predicate": r[1], "object": r[2]}
            for r in rows
        ]

    async def _synthesize_phase(self, clusters: list[list[dict]]) -> list[dict]:
        """Call LLM to generate a synthesis atom per cluster.

        Reads [consolidation] LLM config; falls back to [annotation]
        values for deployments that haven't migrated their config, and
        finally to the NVIDIA NIM defaults. API key pulls from
        CONSOLIDATION_API_KEY (or OPENAI_API_KEY / NVIDIA_API_KEY) env
        vars, or the legacy embedding.api_key TOML field.

        Skips a cluster entirely when an existing observation already has
        the same evidence set — avoids paying the LLM cost to re-synthesize
        a belief we already have.
        """
        import requests
        from .config import resolve_llm_config

        llm = resolve_llm_config('consolidation')
        llm_url = llm['url']
        llm_model = llm['model']
        timeout = llm['timeout']
        api_key = llm['api_key']

        enable_llm = _cfg('consolidation', 'enable_llm', True)
        # When triples extraction is off, ask the LLM only for the
        # OBSERVATION — no point spending tokens on a TRIPLES section
        # we'd parse and discard. Same parser is tolerant of either
        # output shape, so this is purely a prompt simplification.
        ask_for_triples = bool(_cfg('triples', 'enable_extraction', False))

        # P48: canonical predicate + subject vocabulary block, computed
        # once per consolidation pass and injected into every cluster's
        # prompt. Always on when triples extraction is enabled — this
        # is just a prompt-level vocabulary hint, not enforcement, and
        # it's the right default for any deployment that produces
        # triples (the LLM still picks predicates that fit the data;
        # the canonical list reduces aliasing where possible). When
        # triples extraction is off the block is irrelevant (no TRIPLES
        # section asked for), so we skip computing it in that mode.
        vocab_block = ""
        if ask_for_triples:
            try:
                _vb_conn = get_db()
                vocab_block = _canonical_vocab_block(
                    _vb_conn,
                    extra_subjects=getattr(
                        self, '_extra_canonical_subjects', None,
                    ),
                )
                _vb_conn.close()
            except Exception:
                vocab_block = ""

        import re as _re
        _prefix_pat = _re.compile(r"^\[Consolidated from \d+ atoms?\]\s*")

        def _strip_prefix(s: str) -> str:
            return _prefix_pat.sub("", s or "")

        # Each cluster's LLM synthesis is independent (its own source_ids,
        # its own subset/prior-triples reads), so we always fan out across
        # a thread pool. Default 8 in flight — gpt-5.4-nano / haiku / etc.
        # are latency-bound (~1-3s/call), so 8 concurrent turns a
        # 30-cluster question's ~60s serial wall into ~8s. The cap keeps
        # us under per-account concurrent-request limits on standard
        # OpenAI / Anthropic tiers; raise it via [consolidation]
        # parallel_workers if you've upped your limits.
        parallel_workers = max(1, int(_cfg('consolidation', 'parallel_workers', 8)))

        # PREP phase: build (idx, cluster, prompt_or_none) tuples
        # sequentially — needs SQLite reads (existing-obs / subset-obs /
        # prior-triples) that don't parallelize cleanly with sqlite3's
        # default thread-checking. Skipped clusters land with prompt=None.
        prep: list[dict] = []
        skipped_existing = 0
        for cluster_idx, cluster in enumerate(clusters):
            # Strip any prior consolidation prefix from cluster members so
            # re-consolidation doesn't cause "[Consolidated from X] [Consolidated from Y] ..." stacking in either path.
            contents = [_strip_prefix(a['content']) for a in cluster]
            joined = "\n- ".join(contents)
            stream = cluster[0].get('stream', 'semantic')
            source_ids = [a['id'] for a in cluster]

            # Idempotence: skip clusters that already have an observation with
            # the identical source set. Saves the LLM call and prevents
            # duplicate observations from re-running consolidation.
            if self._existing_observation_for_cluster(source_ids) is not None:
                skipped_existing += 1
                continue

            # If the cluster's source set is a strict superset of an existing
            # observation, the new observation will supersede the old one.
            # Collected here so _restructure_phase can write the edges after
            # the new observation atom is stored.
            subset_obs = self._subset_observations_for_cluster(source_ids)

            # When this cluster supersedes prior observations, pull their
            # triples in as "previous beliefs" context for the synthesizer.
            # The LLM gets to decide which still hold, which need updating,
            # and which to drop — informed by both the new evidence in the
            # cluster and the prior conclusions we'd reached on a smaller
            # evidence set. Old triples stay attached to the (about-to-be-
            # demoted) old observation; new triples are emitted as the
            # canonical going-forward set on the new observation.
            prior_triples = self._fetch_prior_triples(subset_obs) if subset_obs else []
            prior_block = ""
            if prior_triples:
                prior_lines = [
                    f"({t['subject']}, {t['predicate']}, {t['object']})"
                    for t in prior_triples
                ]
                if ask_for_triples:
                    prior_block = (
                        "Previous beliefs about these atoms (from earlier "
                        "consolidations on a smaller evidence set):\n"
                        + "\n".join(prior_lines)
                        + "\n\nFor each previous belief: if the new atoms still "
                        "support it, restate it in your TRIPLES section; if the "
                        "new atoms revise or contradict it, output the updated "
                        "version (or omit if it's no longer true).\n\n"
                    )
                else:
                    # Observation-only mode: phrase the prior context
                    # the same way but ask for inclusion in the
                    # observation prose rather than a separate triples
                    # block.
                    prior_block = (
                        "Previous beliefs about these atoms (from earlier "
                        "consolidations on a smaller evidence set):\n"
                        + "\n".join(prior_lines)
                        + "\n\nFor each previous belief: if the new atoms "
                        "still support it, include it in the observation; "
                        "if the new atoms revise or contradict it, surface "
                        "the updated version (or omit if it's no longer "
                        "true).\n\n"
                    )

            # Build the prompt for LLM synthesis (or skip it entirely when
            # enable_llm is off — _finalize_cluster handles the fallback).
            prompt = None
            if enable_llm:
                if ask_for_triples:
                    prompt = (
                        f"You are consolidating {len(cluster)} related memory atoms. "
                        f"Produce THREE outputs in a single response.\n\n"
                        f"Output format (exactly these section headers, in this order):\n\n"
                        f"OBSERVATION:\n"
                        f"<one or two sentences capturing what the atoms collectively convey>\n\n"
                        f"TRIPLES:\n"
                        f"(subject, predicate, object)\n"
                        f"(subject, predicate, object, valid_from=YYYY-MM-DD)\n"
                        f"(subject, predicate, object, valid_from=YYYY-MM-DD, valid_until=YYYY-MM-DD)\n"
                        f"...\n"
                        f"[OR write: NONE if no clean triples]\n\n"
                        f"CONTRADICTIONS:\n"
                        f"<atom_index_a> vs <atom_index_b>: <one-sentence summary "
                        f"of what they disagree on>\n"
                        f"...\n"
                        f"[OR write: NONE if no contradictions]\n\n"
                        f"Rules for the OBSERVATION:\n"
                        f"- Preserve specific dates, times, numbers, names, and direct quotes "
                        f"VERBATIM when they appear in the atoms.\n"
                        f"- If atoms disagree on a fact, keep both versions ('user first "
                        f"mentioned X on date A, then updated to Y on date B').\n"
                        f"- If an atom is dated '[YYYY-MM-DD role] ...', include the date "
                        f"in the observation when the date matters to the content.\n"
                        f"- Do not invent details not present in the atoms.\n\n"
                        f"Rules for TRIPLES:\n"
                        f"- Subject must be a NAMED ENTITY (person, system, tool, place), max 30 chars\n"
                        f"- Object must be a SHORT SPECIFIC VALUE, max 30 chars\n"
                        f"- Predicate must be lowercase_snake_case\n"
                        f"- PREFER reusing canonical intent predicates over inventing\n"
                        f"  domain-specific compounds. Detail goes in the OBJECT, not the\n"
                        f"  predicate. Instead of (User, prefers_podcast_length, 20-30_minutes),\n"
                        f"  emit (User, prefers, podcast_length=20-30_minutes). You MAY\n"
                        f"  introduce a new predicate when no canonical fits — typically for\n"
                        f"  domain relations between two non-User entities, e.g.\n"
                        f"  (CompanyX, manufactures, ProductY).\n"
                        f"- Implicit subject 'User' for user-preference statements\n"
                        f"- Lists become multiple triples (one per item)\n"
                        f"- Skip emotional/philosophical/meta-commentary content (write NONE)\n\n"
                        f"{vocab_block}"
                        f"Rules for TEMPORAL TAGS (optional valid_from/valid_until):\n"
                        f"- Use ONLY when the atoms show a fact CHANGED over time. Take the\n"
                        f"  ``YYYY-MM-DD`` from the dated atom prefix(es).\n"
                        f"- ``valid_from`` only: fact starts on a date and is still current\n"
                        f"  (most user-preference statements). Example: user moves to a new\n"
                        f"  city — emit (User, lives_in, NewCity, valid_from=YYYY-MM-DD).\n"
                        f"- Both bounds: closed interval. Example: user held a job from A to\n"
                        f"  B — emit (User, employed_at, OldJob, valid_from=A, valid_until=B).\n"
                        f"- DO NOT add bounds to facts that don't change (genres, languages,\n"
                        f"  ratings, etc.). DO NOT use the consolidation date — use the\n"
                        f"  source atom's own date.\n\n"
                        f"Rules for CONTRADICTIONS:\n"
                        f"- Only flag *direct* disagreements where two atoms make\n"
                        f"  incompatible claims about the same fact (different\n"
                        f"  objects for the same subject+predicate; opposing\n"
                        f"  preferences on the same topic; incompatible dates).\n"
                        f"- Use 1-based atom indices from the list below.\n"
                        f"- Don't flag temporal evolution (\"used to like X, now likes Y\")\n"
                        f"  — that's a TRIPLES temporal-tag case, not a contradiction.\n"
                        f"- Don't flag stylistic / phrasing differences. Substance only.\n\n"
                        f"{prior_block}"
                        f"Atoms:\n- {joined}"
                    )
                else:
                    # Observation-only — no TRIPLES section, no
                    # triples rules. Saves tokens and shaves LLM
                    # latency on the canonical bench config where
                    # triples persistence is off.
                    prompt = (
                        f"You are consolidating {len(cluster)} related memory atoms. "
                        f"Produce a single observation that captures what the "
                        f"atoms collectively convey.\n\n"
                        f"Output format (exactly this header, then the observation):\n\n"
                        f"OBSERVATION:\n"
                        f"<one or two sentences>\n\n"
                        f"Rules for the OBSERVATION:\n"
                        f"- Preserve specific dates, times, numbers, names, and direct quotes "
                        f"VERBATIM when they appear in the atoms.\n"
                        f"- If atoms disagree on a fact, keep both versions ('user first "
                        f"mentioned X on date A, then updated to Y on date B').\n"
                        f"- If an atom is dated '[YYYY-MM-DD role] ...', include the date "
                        f"in the observation when the date matters to the content.\n"
                        f"- Do not invent details not present in the atoms.\n\n"
                        f"{prior_block}"
                        f"Atoms:\n- {joined}"
                    )

            prep.append({
                "cluster": cluster, "contents": contents, "stream": stream,
                "source_ids": source_ids, "subset_obs": subset_obs,
                "prompt": prompt,
            })

        # LLM phase: fan out the synthesis calls. asyncio.Semaphore-bounded
        # gather of size parallel_workers; submit only the entries with
        # prompts (others land in the fallback path during finalize).
        # Results land in raw_by_idx for the in-order finalize that follows.
        #
        # Async-native after chainlink #47 (Phase 3 of #20). The semaphore
        # caps in-flight LLM calls so we stay under per-account
        # concurrent-request limits; the underlying ``_AsyncClaudePool``
        # adds its own per-runner cap on top of this for the claude_code
        # provider.
        import asyncio
        from ._llm import call_llm as _call_llm
        raw_by_idx: dict[int, str] = {}

        sem = asyncio.Semaphore(parallel_workers)

        async def _do_call(idx: int, prompt_text: str) -> tuple[int, str]:
            async with sem:
                try:
                    text = await _call_llm(
                        llm, prompt=prompt_text,
                        max_tokens=1500, temperature=0.3,
                    )
                    return idx, text
                except Exception as e:
                    logger.warning(f"LLM synthesis failed (cluster {idx}): {e}")
                    return idx, ""

        prompts_to_run = [(i, p["prompt"]) for i, p in enumerate(prep) if p["prompt"]]
        if prompts_to_run:
            tasks = [_do_call(idx, prompt_text)
                     for idx, prompt_text in prompts_to_run]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                # Per-task try/except already swallowed call errors and
                # returned (idx, ""); a True exception here would mean
                # something inside _do_call escaped — preserve the
                # cluster-skip behavior the threading version had.
                if isinstance(r, BaseException):
                    logger.warning(f"LLM synthesis task crashed: {r}")
                    continue
                i, raw = r
                raw_by_idx[i] = raw

        # Finalize phase: parse, fall back, build synthesis records in
        # cluster order so _restructure_phase processes them deterministically.
        syntheses: list[dict] = []
        for idx, p in enumerate(prep):
            cluster = p["cluster"]
            contents = p["contents"]
            stream = p["stream"]
            source_ids = p["source_ids"]
            subset_obs = p["subset_obs"]

            synthesis_content = None
            cluster_triples: list[dict] = []
            cluster_contradictions: list[str] = []
            raw = raw_by_idx.get(idx, "")
            if raw:
                synthesis_content, cluster_triples, cluster_contradictions = (
                    _parse_structured_synthesis(raw)
                )

            # Fallback: take the longest (already-stripped) content as the
            # representative, then wrap with a single-layer prefix.
            if not synthesis_content:
                synthesis_content = f"[Consolidated from {len(cluster)} atoms] {max(contents, key=len)}"
                cluster_triples = []  # no triples without successful LLM output
                cluster_contradictions = []
            else:
                # Defensive: in case the LLM echoed a prefix into its output.
                synthesis_content = _strip_prefix(synthesis_content)

            syntheses.append({
                "content": synthesis_content,
                "stream": stream,
                "source_ids": source_ids,
                "cluster_size": len(cluster),
                "supersedes_observations": subset_obs,
                # P35: triples extracted in the same LLM call as the
                # observation. _restructure_phase persists them with
                # atom_id pointing at the freshly-stored observation.
                # If the LLM didn't emit any (NONE block), this is [].
                "triples": cluster_triples,
                # P35-c (P47 bundle): contradictions the LLM flagged
                # within this cluster — raw single-line strings,
                # surfaced as a structured event in _restructure_phase
                # so the P25 audit cron can pick them up.
                "contradictions": cluster_contradictions,
            })

        self._last_skipped_existing = skipped_existing
        return syntheses

    def _compute_trend_for_cluster(
        self, conn, source_ids: list[str], now_iso: str,
    ) -> str | None:
        """P17 / P47: label the consolidated observation with a trend
        bucket based on how the cluster's source atoms have been
        accessed lately. Pure access-log-driven — no LLM call.

        ratio = retrievals_last_30d / max(retrievals_30_to_90d_ago, 1)

        - ratio > 1.2  → ``improving``  (no penalty, surfaces in P47 promotion)
        - 0.7 ≤ r ≤ 1.2 → ``stable``    (no penalty)
        - 0.3 ≤ r < 0.7 → ``weakening`` (×0.7 retrieval multiplier)
        - ratio < 0.3   → ``stale``     (×0.4 retrieval multiplier;
                                          surfaces in P47 cleanup)

        Returns ``None`` when the cluster has no access history (fresh
        atoms with zero retrievals in the prior 90d) — left as NULL so
        the existing trend multipliers (``saga.core``) treat it as
        unlabeled / no penalty.
        """
        if not source_ids:
            return None
        from datetime import datetime, timedelta, timezone
        try:
            now = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        except ValueError:
            now = datetime.now(timezone.utc)
        cutoff_30 = (now - timedelta(days=30)).isoformat()
        cutoff_90 = (now - timedelta(days=90)).isoformat()

        placeholders = ",".join("?" * len(source_ids))
        try:
            recent_row = conn.execute(
                f"SELECT COUNT(*) FROM access_log "
                f"WHERE atom_id IN ({placeholders}) AND accessed_at >= ?",
                tuple(source_ids) + (cutoff_30,),
            ).fetchone()
            prior_row = conn.execute(
                f"SELECT COUNT(*) FROM access_log "
                f"WHERE atom_id IN ({placeholders}) "
                f"AND accessed_at >= ? AND accessed_at < ?",
                tuple(source_ids) + (cutoff_90, cutoff_30),
            ).fetchone()
        except Exception:
            return None

        recent = int(recent_row[0] if recent_row else 0)
        prior = int(prior_row[0] if prior_row else 0)
        if recent == 0 and prior == 0:
            return None  # no signal — leave NULL

        # Use prior=1 as the floor when prior is genuinely zero so a
        # cold-start cluster with any recent activity reads as
        # "improving" rather than dividing by zero.
        ratio = recent / max(prior, 1)
        if ratio > 1.2:
            return "improving"
        if ratio >= 0.7:
            return "stable"
        if ratio >= 0.3:
            return "weakening"
        return "stale"

    def _restructure_phase(self, syntheses: list[dict]) -> dict:
        """Store synthesis atoms, create atom_relations, reduce source stability."""
        now = datetime.now(timezone.utc).isoformat()

        atoms_stored = 0
        relations_created = 0
        sources_reduced = 0
        observations_superseded = 0
        triples_persisted = 0

        # P35: triples are written when the consolidation prompt extracts
        # them AND [triples] enable_extraction is on. The flag stays
        # gating because some deployments don't want triples in the DB
        # at all (storage / privacy / "we don't use the graph pathway").
        persist_triples = bool(_cfg('triples', 'enable_extraction', False))

        # Phase A: store each synthesis atom (store_atom opens/closes its own connection)
        stored = []
        for syn in syntheses:
            syn_id = store_atom(
                content=syn["content"],
                stream=syn["stream"],
                source_type="consolidation",
                metadata={"consolidated_from": syn["source_ids"][:10],
                          "cluster_size": syn["cluster_size"]},
                memory_type="observation",
                evidence_count=syn["cluster_size"],
            )
            if syn_id is None:
                continue
            atoms_stored += 1
            stored.append((syn_id, syn["source_ids"], syn.get("supersedes_observations", [])))

            # P35: persist triples produced by the same LLM call as
            # this observation. atom_id points at the observation atom
            # so the triple is provenanced to the consolidated belief
            # rather than any single source raw.
            if persist_triples:
                cluster_triples = syn.get("triples") or []
                if cluster_triples:
                    superseded_obs_ids = syn.get("supersedes_observations") or []
                    triples_persisted += self._persist_consolidation_triples(
                        cluster_triples, syn_id, superseded_obs_ids
                    )

        # Phase B: create relations and reduce stability in a single transaction.
        # Two edge directions:
        #   raw -> observation  (consolidated_into)  — legacy, used by spread activation
        #   observation -> raw  (evidenced_by)       — new, used by P9 evidence boost
        #
        # CR#16: every per-edge / per-stability / per-trend write was
        # wrapped in try/except: pass. A real failure mid-batch left
        # the consolidation half-recorded — some sources demoted, some
        # not; some edges present, some missing — without ever
        # surfacing the error. One transaction makes the whole Phase B
        # batch atomic; any error rolls the entire phase back so the
        # caller sees the failure and can retry cleanly.
        from .core import transactional

        trends_labeled = 0
        trends_breakdown: dict[str, int] = {}
        with transactional() as conn:
            for syn_id, source_ids, supersedes_obs in stored:
                for source_id in source_ids:
                    conn.execute("""
                        INSERT OR IGNORE INTO atom_relations
                            (source_id, target_id, relation_type, confidence, created_at)
                        VALUES (?, ?, 'consolidated_into', 1.0, ?)
                    """, (source_id, syn_id, now))
                    relations_created += 1
                    conn.execute("""
                        INSERT OR IGNORE INTO atom_relations
                            (source_id, target_id, relation_type, confidence, created_at)
                        VALUES (?, ?, 'evidenced_by', 1.0, ?)
                    """, (syn_id, source_id, now))
                    relations_created += 1

                for source_id in source_ids:
                    conn.execute(
                        "UPDATE atoms SET stability = stability * ? WHERE id = ?",
                        (self.stability_reduction, source_id)
                    )
                    sources_reduced += 1

                # The new observation supersedes any prior observation whose
                # evidence set is a strict subset. The retrieval-side
                # demotion (saga.core._apply_supersedes_demotion) handles
                # the score penalty.
                for old_obs_id in supersedes_obs:
                    conn.execute("""
                        INSERT OR IGNORE INTO atom_relations
                            (source_id, target_id, relation_type, confidence, created_at, metadata)
                        VALUES (?, ?, 'supersedes', 1.0, ?, ?)
                    """, (syn_id, old_obs_id, now,
                          json.dumps({"trigger": "consolidation"})))
                    observations_superseded += 1
                    relations_created += 1

            # P17 / P47: label each consolidated observation with a
            # trend bucket based on the cluster's source-atom access
            # patterns. Activates the previously-no-op trend multipliers
            # in saga.core retrieval AND feeds P47's promotion /
            # demotion candidate selection. NULL trend means
            # "no signal," same as today.
            for syn_id, source_ids, _ in stored:
                trend = self._compute_trend_for_cluster(conn, source_ids, now)
                if trend is None:
                    continue
                conn.execute(
                    "UPDATE atoms SET trend = ? WHERE id = ?",
                    (trend, syn_id),
                )
                trends_labeled += 1
                trends_breakdown[trend] = trends_breakdown.get(trend, 0) + 1

        # P35-c (P47 bundle): aggregate contradictions across all
        # clusters and surface as a single structured field on the
        # consolidate response. The P25 audit cron consumes these to
        # emit per-cluster algedonic events.
        contradictions_flagged: list[dict] = []
        for syn, (syn_id, source_ids, _) in zip(syntheses, stored):
            for line in syn.get("contradictions") or []:
                contradictions_flagged.append({
                    "observation_id": syn_id,
                    "source_atom_ids": source_ids,
                    "summary": line,
                })

        return {
            "synthesis_atoms_stored": atoms_stored,
            "relations_created": relations_created,
            "source_atoms_reduced": sources_reduced,
            "observations_superseded": observations_superseded,
            # P35: triples extracted in the consolidation pass and
            # persisted as graph-pathway-eligible facts.
            "triples_persisted": triples_persisted,
            # P17 / P47: trend buckets written this run. Surfaced so
            # the cron summary line can include "labeled N trends."
            "trends_labeled": trends_labeled,
            "trends_breakdown": trends_breakdown,
            # P35-c / P47: per-cluster contradictions the LLM flagged.
            "contradictions_flagged": contradictions_flagged,
        }
