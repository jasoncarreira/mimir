"""
MSAM Consolidation Engine -- Sleep-inspired memory consolidation.

Clusters similar atoms, synthesizes abstractions via LLM, and reduces
source atom stability. This is how human memory works during sleep:
50 similar atoms become 1 abstraction.

Also a critical scaling strategy -- keeps the active atom count bounded
regardless of ingestion rate.

Usage:
    from msam.consolidation import ConsolidationEngine
    engine = ConsolidationEngine()
    result = engine.consolidate(dry_run=True)
"""

import json
import logging
import hashlib
from datetime import datetime, timezone

from .config import get_config
from .core import get_db, store_atom, embed_text, pack_embedding, unpack_embedding

logger = logging.getLogger("msam.consolidation")

_cfg = get_config()

# ─── Configuration ──────────────────────────────────────────────

DEFAULT_SIMILARITY_THRESHOLD = _cfg('consolidation', 'similarity_threshold', 0.80)
DEFAULT_MIN_CLUSTER_SIZE = _cfg('consolidation', 'min_cluster_size', 3)
DEFAULT_MAX_CLUSTERS = _cfg('consolidation', 'max_clusters_per_run', 50)
DEFAULT_STABILITY_REDUCTION = _cfg('consolidation', 'stability_reduction_factor', 0.5)


# ─── P35: structured-output parsing for consolidation ───────────

def _parse_structured_synthesis(text: str) -> tuple[str | None, list[dict]]:
    """Parse the OBSERVATION + TRIPLES dual-output format.

    Returns (observation_text_or_None, list_of_triple_dicts). On parse
    failure, returns (None, []) — caller falls back to longest-source-
    atom representative. If only OBSERVATION parses cleanly, returns
    (observation, []) — graceful degradation when the LLM omits or
    malforms the TRIPLES section.

    Triples are returned without atom_id; the caller fills it in once
    the observation is stored.
    """
    import re
    if not text:
        return None, []

    # Find the OBSERVATION section header. Tolerant of leading
    # whitespace and either `OBSERVATION:` or `**OBSERVATION:**`.
    obs_match = re.search(r'(?im)^\s*\**\s*OBSERVATION\s*:?\s*\**\s*\n?', text)
    if not obs_match:
        # No header — assume the whole response is the observation
        # (legacy single-output format).
        return text.strip() or None, []

    after_obs = text[obs_match.end():]

    # Find the TRIPLES section, which terminates the observation.
    tri_match = re.search(r'(?im)^\s*\**\s*TRIPLES\s*:?\s*\**\s*\n?', after_obs)
    if tri_match:
        observation = after_obs[:tri_match.start()].strip()
        triples_block = after_obs[tri_match.end():].strip()
    else:
        observation = after_obs.strip()
        triples_block = ""

    if not observation:
        observation = None

    # Strip any further section headers from the triples block (some
    # models emit trailing CONTRADICTIONS: or similar that we don't
    # consume yet).
    triples_block = re.split(
        r'(?im)^\s*\**\s*(?:CONTRADICTIONS|NOTES|EXPLANATION)\s*:?\s*\**\s*\n',
        triples_block,
    )[0]

    triples: list[dict] = []
    if triples_block and "NONE" not in triples_block.upper().split("\n")[0]:
        # Same triple shape as triples._parse_triples — reuse its
        # validation. Pass empty atom_id; caller fills it in.
        from .triples import _parse_triples
        triples = _parse_triples(triples_block, atom_id="")

    return observation, triples


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

    def consolidate(self, dry_run: bool = False, max_clusters: int = None) -> dict:
        """Main entry: cluster -> synthesize -> restructure.

        Args:
            dry_run: If True, detect clusters but don't synthesize or restructure.
            max_clusters: Override max clusters for this run.

        Returns:
            Dict with clusters_found, clusters_consolidated, atoms_affected, etc.
        """
        max_clusters = max_clusters or self.max_clusters

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
        syntheses = self._synthesize_phase(clusters)

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

        conn = get_db()
        try:
            # Pre-pass: which prepared tids are not yet in the DB? Those
            # are the ones that need fresh embeddings. Batching the
            # embedding calls turns N sequential ~500ms API roundtrips
            # into one ~600ms batch — observed ~4.6s/cluster overhead
            # on the P41-only bench was dominated by this.
            placeholders = ",".join("?" * len(prepared))
            existing_rows = conn.execute(
                f"SELECT id FROM triples WHERE id IN ({placeholders})",
                tuple(tid for tid, _ in prepared),
            ).fetchall()
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

            for tid, t in prepared:
                row = conn.execute(
                    "SELECT atom_id FROM triples WHERE id = ?", (tid,)
                ).fetchone()

                if row is None:
                    # Fresh insert — embedding pulled from the batch above.
                    embedding = embeddings_by_tid.get(tid)
                    try:
                        conn.execute(
                            "INSERT OR IGNORE INTO triples "
                            "(id, atom_id, subject, predicate, object, "
                            " confidence, embedding, created_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (tid, new_obs_id, t["subject"], t["predicate"],
                             t["object"], float(t.get("confidence", 1.0)),
                             embedding, now),
                        )
                        persisted += 1
                    except Exception:
                        pass
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
                    try:
                        conn.execute(
                            "UPDATE triples SET atom_id = ?, created_at = ? WHERE id = ?",
                            (new_obs_id, now, tid),
                        )
                        persisted += 1
                    except Exception:
                        pass
                # else: triple is attested by some other unrelated
                # observation. Leave it alone — content-level dedup is
                # the right default outside the supersedes window.

            conn.commit()
        finally:
            conn.close()

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

    def _synthesize_phase(self, clusters: list[list[dict]]) -> list[dict]:
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

        import re as _re
        _prefix_pat = _re.compile(r"^\[Consolidated from \d+ atoms?\]\s*")

        def _strip_prefix(s: str) -> str:
            return _prefix_pat.sub("", s or "")

        syntheses = []
        skipped_existing = 0
        for cluster in clusters:
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
                prior_block = (
                    "Previous beliefs about these atoms (from earlier "
                    "consolidations on a smaller evidence set):\n"
                    + "\n".join(prior_lines)
                    + "\n\nFor each previous belief: if the new atoms still "
                    "support it, restate it in your TRIPLES section; if the "
                    "new atoms revise or contradict it, output the updated "
                    "version (or omit if it's no longer true).\n\n"
                )

            # Try LLM synthesis (skipped entirely when consolidation.enable_llm = false)
            synthesis_content = None
            cluster_triples: list[dict] = []
            if enable_llm:
                try:
                    prompt = (
                        f"You are consolidating {len(cluster)} related memory atoms. "
                        f"Produce TWO outputs in a single response.\n\n"
                        f"Output format (exactly these section headers, in this order):\n\n"
                        f"OBSERVATION:\n"
                        f"<one or two sentences capturing what the atoms collectively convey>\n\n"
                        f"TRIPLES:\n"
                        f"(subject, predicate, object)\n"
                        f"(subject, predicate, object)\n"
                        f"...\n"
                        f"[OR write: NONE if no clean triples]\n\n"
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
                        f"- Implicit subject 'User' for user-preference statements\n"
                        f"- Lists become multiple triples (one per item)\n"
                        f"- Skip emotional/philosophical/meta-commentary content (write NONE)\n\n"
                        f"{prior_block}"
                        f"Atoms:\n- {joined}"
                    )
                    resp = requests.post(
                        llm_url,
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": llm_model,
                            "messages": [{"role": "user", "content": prompt}],
                            # Bumped from 300 to fit the structured format —
                            # observation + ~20 triples + reasoning headroom
                            # for reasoning models. Newer OpenAI models
                            # (gpt-5.x) require max_completion_tokens
                            # instead of max_tokens; older models accept
                            # both. Safer to use the newer name.
                            "max_completion_tokens": 1500,
                            "temperature": 0.3,
                        },
                        timeout=timeout,
                    )
                    if resp.status_code != 200:
                        # Don't swallow non-200s silently — that hid a
                        # max_tokens vs max_completion_tokens regression
                        # on gpt-5.4-nano for ~3 bench runs (every P41
                        # configuration produced 0 triples). Logging the
                        # status + body makes the next break obvious.
                        logger.warning(
                            "consolidation LLM returned %d: %s",
                            resp.status_code, resp.text[:300],
                        )
                    else:
                        data = resp.json()
                        msg = data["choices"][0]["message"]
                        # Reasoning models put output in `reasoning`
                        # when content is None.
                        raw = (msg.get("content") or msg.get("reasoning") or "").strip()
                        synthesis_content, cluster_triples = _parse_structured_synthesis(raw)
                except Exception as e:
                    logger.warning(f"LLM synthesis failed: {e}")

            # Fallback: take the longest (already-stripped) content as the
            # representative, then wrap with a single-layer prefix.
            if not synthesis_content:
                synthesis_content = f"[Consolidated from {len(cluster)} atoms] {max(contents, key=len)}"
                cluster_triples = []  # no triples without successful LLM output
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
            })

        self._last_skipped_existing = skipped_existing
        return syntheses

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

        # Phase B: create relations and reduce stability in a single connection.
        # Two edge directions:
        #   raw -> observation  (consolidated_into)  — legacy, used by spread activation
        #   observation -> raw  (evidenced_by)       — new, used by P9 evidence boost
        conn = get_db()
        try:
            for syn_id, source_ids, supersedes_obs in stored:
                for source_id in source_ids:
                    try:
                        conn.execute("""
                            INSERT OR IGNORE INTO atom_relations
                                (source_id, target_id, relation_type, confidence, created_at)
                            VALUES (?, ?, 'consolidated_into', 1.0, ?)
                        """, (source_id, syn_id, now))
                        relations_created += 1
                    except Exception:
                        pass
                    try:
                        conn.execute("""
                            INSERT OR IGNORE INTO atom_relations
                                (source_id, target_id, relation_type, confidence, created_at)
                            VALUES (?, ?, 'evidenced_by', 1.0, ?)
                        """, (syn_id, source_id, now))
                        relations_created += 1
                    except Exception:
                        pass

                for source_id in source_ids:
                    try:
                        conn.execute(
                            "UPDATE atoms SET stability = stability * ? WHERE id = ?",
                            (self.stability_reduction, source_id)
                        )
                        sources_reduced += 1
                    except Exception:
                        pass

                # The new observation supersedes any prior observation whose
                # evidence set is a strict subset. The retrieval-side
                # demotion (msam.core._apply_supersedes_demotion) handles
                # the score penalty.
                for old_obs_id in supersedes_obs:
                    try:
                        conn.execute("""
                            INSERT OR IGNORE INTO atom_relations
                                (source_id, target_id, relation_type, confidence, created_at, metadata)
                            VALUES (?, ?, 'supersedes', 1.0, ?, ?)
                        """, (syn_id, old_obs_id, now,
                              json.dumps({"trigger": "consolidation"})))
                        observations_superseded += 1
                        relations_created += 1
                    except Exception:
                        pass

            conn.commit()
        finally:
            conn.close()

        return {
            "synthesis_atoms_stored": atoms_stored,
            "relations_created": relations_created,
            "source_atoms_reduced": sources_reduced,
            "observations_superseded": observations_superseded,
            # P35: triples extracted in the consolidation pass and
            # persisted as graph-pathway-eligible facts.
            "triples_persisted": triples_persisted,
        }
