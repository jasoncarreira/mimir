"""
SAGA Decay -- Forgetting and lifecycle management.

Implements stability-based retrievability decay (ACT-R inspired),
state transitions (active -> fading -> dormant), profile compaction,
and token budget monitoring.

Never deletes atoms. State transitions only. Data is preserved.
"""

import math
import logging
import sys
import os
from datetime import datetime, timezone

# Ensure saga/ is importable when called directly

from .config import get_config as _get_config
_cfg = _get_config()

from .core import get_db, get_stats

logger = logging.getLogger("saga.decay")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

# ─── Constants ────────────────────────────────────────────────────

# State transition thresholds -- read from config
THRESHOLD_ACTIVE_TO_FADING = _cfg('decay', 'active_to_fading_threshold', 0.3)
THRESHOLD_FADING_TO_DORMANT = _cfg('decay', 'fading_to_dormant_threshold', 0.1)

# Profile compaction thresholds -- read from config
COMPACTION_FULL_TO_STANDARD_MIN_AGE_DAYS = _cfg('decay', 'compaction_full_min_age_days', 7)
COMPACTION_FULL_TO_STANDARD_MAX_ACCESS = _cfg('decay', 'compaction_full_max_access', 3)
COMPACTION_STANDARD_TO_LIGHTWEIGHT_MIN_AGE_DAYS = _cfg('decay', 'compaction_standard_min_age_days', 14)
COMPACTION_STANDARD_TO_LIGHTWEIGHT_MAX_ACCESS = _cfg('decay', 'compaction_standard_max_access', 2)

PROFILE_TARGET_CHARS = {
    "lightweight": _cfg('decay', 'profile_target_lightweight_chars', 90),
    "standard": _cfg('decay', 'profile_target_standard_chars', 240),
}
COMPACTION_TRIGGER_RATIO = _cfg('decay', 'compaction_trigger_ratio', 1.5)

# Protection: never transition atoms accessed within N days
PROTECTION_DAYS = _cfg('decay', 'protection_days', 7)

# Token budget ceiling -- read from config
TOKEN_BUDGET = _cfg('storage', 'token_budget_ceiling', 40_000)


# ─── Core Decay Functions ─────────────────────────────────────────

def compute_all_retrievability() -> int:
    """
    Recompute retrievability for every active/fading atom.

    R = e^(-age_hours / (stability * 168))

    168 = hours in a week (same as core.py compute_activation).
    Uses SQLite user-defined function for math.exp to push computation into SQL.
    Returns number of atoms updated.
    """
    conn = get_db()
    now_iso = datetime.now(timezone.utc).isoformat()

    # Register math.exp as a SQLite function for in-DB computation
    conn.create_function("exp", 1, math.exp)

    try:
        cursor = conn.execute("""
            UPDATE atoms SET retrievability = exp(
                -MAX((julianday(?) - julianday(created_at)) * 24.0, 0.01)
                / (MAX(COALESCE(stability, 1.0), 0.01) * 168.0)
            )
            WHERE state IN ('active', 'fading')
        """, (now_iso,))
        updated = cursor.rowcount
        conn.commit()
    except Exception as e:
        logger.warning(f"SQL-based retrievability failed, falling back to Python: {e}")
        # Fallback to Python loop
        now = datetime.now(timezone.utc)
        rows = conn.execute(
            "SELECT id, created_at, stability FROM atoms WHERE state IN ('active', 'fading')"
        ).fetchall()
        updated = 0
        batch = []
        for row in rows:
            try:
                created = datetime.fromisoformat(row["created_at"])
                age_hours = max((now - created).total_seconds() / 3600, 0.01)
                stability = row["stability"] if row["stability"] and row["stability"] > 0 else 1.0
                retrievability = math.exp(-age_hours / (stability * 168))
                batch.append((retrievability, row["id"]))
                updated += 1
            except Exception:
                pass
        if batch:
            conn.executemany("UPDATE atoms SET retrievability = ? WHERE id = ?", batch)
            conn.commit()

    conn.close()
    logger.info(f"compute_all_retrievability: updated {updated} atoms")
    return updated


def transition_states() -> dict:
    """
    Move atoms between states based on retrievability.
    
    Rules:
    - active -> fading: R < 0.3
    - fading -> dormant: R < 0.1
    - Never transition atoms accessed in last 7 days (protection window)
    
    Returns dict with counts of transitions made.
    """
    from .core import transactional, log_forgetting, _fire_hook

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    # Protection: find atom IDs accessed in last N days
    from datetime import timedelta
    protection_cutoff = now - timedelta(days=PROTECTION_DAYS)
    protection_cutoff_iso = protection_cutoff.isoformat()

    faded = 0
    dormanted = 0
    log_entries = []
    protected_ids: set[str] = set()
    fired_hooks: list[tuple[str, str, str]] = []

    # CR#16: each transition is two writes (UPDATE atoms +
    # forgetting_log INSERT via log_forgetting). Without one txn a
    # mid-batch failure leaves rows in the new state but with no
    # forgetting_log audit row, or the audit row but not the state
    # change — the decay history view would silently disagree with
    # the atoms table. Hooks fire after commit so listeners only see
    # transitions that actually persisted.
    with transactional() as conn:
        # Single UNION query for all protected IDs (replaces 3 separate queries)
        protected_rows = conn.execute("""
            SELECT DISTINCT id FROM (
                SELECT DISTINCT atom_id AS id FROM access_log WHERE accessed_at >= ?
                UNION SELECT id FROM atoms WHERE last_accessed_at >= ?
                UNION SELECT id FROM atoms WHERE is_pinned = 1
            )
        """, (protection_cutoff_iso, protection_cutoff_iso)).fetchall()
        protected_ids = {r["id"] for r in protected_rows}

        # active -> fading
        active_rows = conn.execute(
            "SELECT id, retrievability FROM atoms WHERE state = 'active' AND retrievability < ?",
            (THRESHOLD_ACTIVE_TO_FADING,)
        ).fetchall()

        for row in active_rows:
            if row["id"] in protected_ids:
                logger.debug(f"  Protected from fading: {row['id'][:8]} (recently accessed)")
                continue
            conn.execute(
                "UPDATE atoms SET state = 'fading' WHERE id = ?",
                (row["id"],)
            )
            log_forgetting(conn, row["id"], "active", "fading",
                          f"retrievability {row['retrievability']:.4f} below threshold {THRESHOLD_ACTIVE_TO_FADING}",
                          {"retrievability": round(row['retrievability'], 4), "threshold": THRESHOLD_ACTIVE_TO_FADING})
            fired_hooks.append((row["id"], "active", "fading"))
            log_entries.append(f"active->fading: {row['id'][:8]} R={row['retrievability']:.4f}")
            faded += 1

        # fading -> dormant
        fading_rows = conn.execute(
            "SELECT id, retrievability FROM atoms WHERE state = 'fading' AND retrievability < ?",
            (THRESHOLD_FADING_TO_DORMANT,)
        ).fetchall()

        for row in fading_rows:
            if row["id"] in protected_ids:
                logger.debug(f"  Protected from dormant: {row['id'][:8]} (recently accessed)")
                continue
            conn.execute(
                "UPDATE atoms SET state = 'dormant' WHERE id = ?",
                (row["id"],)
            )
            log_forgetting(conn, row["id"], "fading", "dormant",
                          f"retrievability {row['retrievability']:.4f} below threshold {THRESHOLD_FADING_TO_DORMANT}",
                          {"retrievability": round(row['retrievability'], 4), "threshold": THRESHOLD_FADING_TO_DORMANT})
            fired_hooks.append((row["id"], "fading", "dormant"))
            log_entries.append(f"fading->dormant: {row['id'][:8]} R={row['retrievability']:.4f}")
            dormanted += 1

    # Fire hooks after commit so listeners only see persisted transitions.
    for atom_id, prev_state, new_state in fired_hooks:
        _fire_hook('on_decay', atom_id=atom_id, previous_state=prev_state, new_state=new_state)

    if log_entries:
        for entry in log_entries:
            logger.info(f"  state_transition: {entry}")

    result = {
        "faded": faded,
        "dormanted": dormanted,
        "protected": len(protected_ids),
    }
    logger.info(f"transition_states: faded={faded} dormanted={dormanted} protected={len(protected_ids)}")
    return result


def compact_profiles() -> dict:
    """
    Compress atom profiles to save tokens.
    
    Rules:
    - full -> standard: access_count < 3 AND age > 7 days
    - standard -> lightweight: access_count < 2 AND age > 14 days
    
    Only compact if content exceeds target by > 50%.
    Truncation is hard truncation (no ellipsis to preserve token budget).
    
    Returns dict with compaction counts and tokens freed.
    """
    from .core import transactional

    now = datetime.now(timezone.utc)

    compacted_to_standard = 0
    compacted_to_lightweight = 0
    tokens_freed = 0
    log_entries = []

    # CR#16: profile-compaction is N UPDATE atoms in a sweep. A
    # partial commit would leave some atoms compacted and the
    # tokens_freed accounting (returned to the caller) overstate the
    # actual freed bytes — the next decay run would re-evaluate the
    # un-committed atoms with old content.
    with transactional() as conn:
        # full -> standard
        full_rows = conn.execute(
            """SELECT id, content, access_count, created_at FROM atoms
               WHERE profile = 'full' AND state IN ('active', 'fading', 'dormant')"""
        ).fetchall()

        for row in full_rows:
            created = datetime.fromisoformat(row["created_at"])
            age_days = (now - created).total_seconds() / 86400

            if age_days < COMPACTION_FULL_TO_STANDARD_MIN_AGE_DAYS:
                continue
            if row["access_count"] >= COMPACTION_FULL_TO_STANDARD_MAX_ACCESS:
                continue

            content = row["content"]
            target = PROFILE_TARGET_CHARS["standard"]

            if len(content) <= target * COMPACTION_TRIGGER_RATIO:
                continue  # not worth compacting

            old_len = len(content)
            new_content = content[:target]
            freed = (old_len - len(new_content)) // 4  # rough token estimate

            conn.execute(
                "UPDATE atoms SET profile = 'standard', content = ? WHERE id = ?",
                (new_content, row["id"])
            )
            tokens_freed += freed
            compacted_to_standard += 1
            log_entries.append(
                f"full->standard: {row['id'][:8]} age={age_days:.1f}d access={row['access_count']} "
                f"chars={old_len}->{len(new_content)} freed~{freed}tok"
            )

        # standard -> lightweight
        std_rows = conn.execute(
            """SELECT id, content, access_count, created_at FROM atoms
               WHERE profile = 'standard' AND state IN ('active', 'fading', 'dormant')"""
        ).fetchall()

        for row in std_rows:
            created = datetime.fromisoformat(row["created_at"])
            age_days = (now - created).total_seconds() / 86400

            if age_days < COMPACTION_STANDARD_TO_LIGHTWEIGHT_MIN_AGE_DAYS:
                continue
            if row["access_count"] >= COMPACTION_STANDARD_TO_LIGHTWEIGHT_MAX_ACCESS:
                continue

            content = row["content"]
            target = PROFILE_TARGET_CHARS["lightweight"]

            if len(content) <= target * COMPACTION_TRIGGER_RATIO:
                continue

            old_len = len(content)
            new_content = content[:target]
            freed = (old_len - len(new_content)) // 4

            conn.execute(
                "UPDATE atoms SET profile = 'lightweight', content = ? WHERE id = ?",
                (new_content, row["id"])
            )
            tokens_freed += freed
            compacted_to_lightweight += 1
            log_entries.append(
                f"std->lightweight: {row['id'][:8]} age={age_days:.1f}d access={row['access_count']} "
                f"chars={old_len}->{len(new_content)} freed~{freed}tok"
            )

    if log_entries:
        for entry in log_entries:
            logger.info(f"  compaction: {entry}")

    total_compacted = compacted_to_standard + compacted_to_lightweight
    result = {
        "compacted_to_standard": compacted_to_standard,
        "compacted_to_lightweight": compacted_to_lightweight,
        "total_compacted": total_compacted,
        "tokens_freed": tokens_freed,
    }
    logger.info(
        f"compact_profiles: to_std={compacted_to_standard} to_light={compacted_to_lightweight} "
        f"tokens_freed={tokens_freed}"
    )
    return result


def budget_check() -> dict:
    """
    Check current token budget utilization.
    
    Returns dict with: total_tokens, budget_pct, recommendation.
    """
    stats = get_stats()
    total_tokens = stats["est_active_tokens"]
    budget_pct = (total_tokens / TOKEN_BUDGET) * 100

    if budget_pct > 95:
        recommendation = "EMERGENCY: tombstone lowest-activation atoms immediately"
    elif budget_pct > 85:
        recommendation = "CRITICAL: run aggressive decay with lower thresholds"
    elif budget_pct > 70:
        recommendation = "WARNING: run compaction to free tokens"
    else:
        recommendation = "OK: budget within normal range"

    result = {
        "total_tokens": total_tokens,
        "budget_pct": round(budget_pct, 2),
        "budget_ceiling": TOKEN_BUDGET,
        "recommendation": recommendation,
        "active_atoms": stats["active_atoms"],
        "total_atoms": stats["total_atoms"],
    }

    logger.info(
        f"budget_check: {total_tokens}/{TOKEN_BUDGET} tokens ({budget_pct:.1f}%) -- {recommendation}"
    )
    return result


# ─── Full Decay Cycle ─────────────────────────────────────────────

# VSM: S3 (saga-internal) — atom stability decays over time;
#      access_count + contributed bumps stability. Below thresholds
#      atoms transition active → fading → dormant → tombstone.
#      Retrieval respects state filter so low-value detail naturally
#      fades out without explicit deletion.
# loop_id: 4.4
def run_decay_cycle() -> dict:
    """
    Full decay cycle -- designed to be called by systemd timer.
    
    Steps:
    1. compute_all_retrievability()
    2. transition_states()
    3. compact_profiles()
    4. budget_check() (before and after)
    5. Log results to metrics
    
    Returns summary dict.
    """
    import time
    start = time.time()

    logger.info("=== SAGA Decay Cycle Start ===")

    # Step 1: Budget check BEFORE (for delta measurement)
    budget_before = budget_check()

    # Step 2: Recompute retrievability
    atoms_updated = compute_all_retrievability()

    # Step 3: State transitions
    transitions = transition_states()

    # Step 4: Profile compaction
    compaction = compact_profiles()

    # Step 4.5: Update confidence gradient from evidence
    try:
        from .core import update_confidence_from_evidence, compute_retrieval_adjustments, expire_negatives, decay_confidence, is_pinned
        confidence_result = update_confidence_from_evidence()
        logger.info(f"confidence_update: {confidence_result['triples_updated']} triples updated, "
                    f"{confidence_result['multi_source_facts']} multi-source facts")

        # Self-improving retrieval: adjust stability based on contribution history
        feedback_result = compute_retrieval_adjustments()
        logger.info(f"retrieval_feedback: {feedback_result['adjustments_made']} adjustments, "
                    f"{feedback_result['over_retrieved_count']} over-retrieved, "
                    f"{feedback_result['high_value_count']} high-value")
        
        # Expire old negative knowledge
        neg_expired = expire_negatives()
        if neg_expired:
            logger.info(f"negative_knowledge: {neg_expired} expired")
        
        # Time-based confidence decay
        conf_decay_result = decay_confidence()
        if conf_decay_result['decayed'] > 0:
            logger.info(f"confidence_decay: {conf_decay_result['decayed']} atoms decayed, "
                       f"{conf_decay_result['exempt_pinned']} pinned, "
                       f"{conf_decay_result['exempt_recent']} recent")
    except Exception as e:
        logger.warning(f"confidence/feedback update failed: {e}")
        confidence_result = {"triples_updated": 0}
        feedback_result = {"adjustments_made": 0}

    # Step 4.6: Intentional forgetting engine
    forgetting_result = {"total_candidates": 0, "actions_taken": 0}
    try:
        if _cfg('decay', 'intentional_forgetting_enabled', False):
            from .forgetting import identify_forgetting_candidates
            _mode = _cfg('decay', 'intentional_forgetting_mode', 'flag')
            forgetting_result = identify_forgetting_candidates(
                dry_run=(_mode != "auto"),
            )
            logger.info(
                f"forgetting_engine: {forgetting_result['total_candidates']} candidates, "
                f"{forgetting_result['actions_taken']} actions"
            )
    except Exception as e:
        logger.warning(f"Intentional forgetting failed: {e}")

    # Step 4.7: Resolve contradictions to supersedes edges (P4-bench prod path).
    # The retrieval-side demotion is always-on; this is the periodic producer.
    # Cheap relative to the rest of the cycle (FAISS top-K + heuristic checks).
    supersedes_result = {"contradictions_found": 0, "supersedes_written": 0}
    try:
        if _cfg('decay', 'auto_resolve_supersedes', True):
            from .core import resolve_contradictions_to_supersedes
            supersedes_result = resolve_contradictions_to_supersedes(
                threshold=_cfg('decay', 'supersedes_resolution_threshold', 0.85),
            )
            if supersedes_result.get("supersedes_written", 0) > 0:
                logger.info(
                    f"supersedes: {supersedes_result['supersedes_written']} edges written "
                    f"({supersedes_result['contradictions_found']} contradictions detected)"
                )
    except Exception as e:
        logger.warning(f"Supersedes resolution failed: {e}")

    # Step 5: Budget check AFTER
    budget_after = budget_check()

    elapsed = time.time() - start

    # Get current state counts for metrics
    conn = get_db()
    active_count = conn.execute("SELECT COUNT(*) FROM atoms WHERE state = 'active'").fetchone()[0]
    fading_count = conn.execute("SELECT COUNT(*) FROM atoms WHERE state = 'fading'").fetchone()[0]
    dormant_count = conn.execute("SELECT COUNT(*) FROM atoms WHERE state = 'dormant'").fetchone()[0]
    conn.close()

    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": round(elapsed, 3),
        "atoms_retrievability_updated": atoms_updated,
        "atoms_faded": transitions["faded"],
        "atoms_dormanted": transitions["dormanted"],
        "atoms_protected": transitions["protected"],
        "atoms_compacted": compaction["total_compacted"],
        "compacted_to_standard": compaction["compacted_to_standard"],
        "compacted_to_lightweight": compaction["compacted_to_lightweight"],
        "tokens_freed": compaction["tokens_freed"],
        "budget_before_pct": budget_before["budget_pct"],
        "budget_after_pct": budget_after["budget_pct"],
        "total_active": active_count,
        "total_fading": fading_count,
        "total_dormant": dormant_count,
        "budget_recommendation": budget_after["recommendation"],
        "forgetting_candidates": forgetting_result["total_candidates"],
        "forgetting_actions": forgetting_result["actions_taken"],
    }

    logger.info(f"=== SAGA Decay Cycle Complete: {elapsed:.3f}s ===")
    logger.info(f"  faded={transitions['faded']} dormanted={transitions['dormanted']} "
                f"compacted={compaction['total_compacted']} tokens_freed={compaction['tokens_freed']} "
                f"budget={budget_after['budget_pct']:.1f}%")

    # Step 6: Log to metrics
    try:
        from .metrics import log_decay_event
        log_decay_event(
            atoms_faded=transitions["faded"],
            atoms_dormant=transitions["dormanted"],
            atoms_compacted=compaction["total_compacted"],
            tokens_freed=compaction["tokens_freed"],
            budget_before=budget_before["budget_pct"],
            budget_after=budget_after["budget_pct"],
            total_active=active_count,
            total_fading=fading_count,
            total_dormant=dormant_count,
        )
    except Exception as e:
        logger.warning(f"Failed to log decay metrics: {e}")
        summary["metrics_error"] = str(e)

    return summary


# ─── Entry Point ─────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    result = run_decay_cycle()
    print(json.dumps(result, indent=2))
