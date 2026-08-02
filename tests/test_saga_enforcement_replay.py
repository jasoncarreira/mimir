from __future__ import annotations

import json
import sqlite3
import struct
from datetime import datetime, timezone
from pathlib import Path

from mimir.saga.client import SagaStore
from mimir.saga.enforcement_replay import analyze_paths
from mimir.saga.legacy_acl_migration import migrate_copy, migrate_operator_rows
from mimir.saga.store import store


def _embedding(_content: str):
    return struct.pack("4f", 1.0, 0.0, 0.0, 0.0), "test", "test", 4


def _atom(saga: SagaStore, content: str, **kwargs) -> str:
    return store(saga.connection(), content, embed_fn=_embedding, **kwargs).atom_id


def test_replay_reports_pre_and_post_fusion_losses_by_caller_surface_and_age(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "saga.db"
    saga = SagaStore(db_path=db_path, embedding_dim=4)
    historical = _atom(saga, "historical legacy")
    still_produced = _atom(saga, "new legacy")
    owned = _atom(
        saga, "alice private", owner_principal="alice", visibility="private",
    )
    public = _atom(
        saga, "public", owner_principal="bob", visibility="public",
    )
    service = _atom(
        saga, "service", owner_principal="service:github", visibility="service",
        origin_domain="github",
    )
    saga.connection().execute(
        "UPDATE atoms SET created_at = ? WHERE id = ?",
        ("2026-01-01T00:00:00+00:00", historical),
    )
    saga.connection().execute(
        "UPDATE atoms SET created_at = ? WHERE id = ?",
        ("2026-07-02T00:00:00+00:00", still_produced),
    )
    saga.connection().commit()

    events = [
        {
            "ts": "2026-07-10T10:00:00Z",
            "surface": "automatic_recall",
            "caller_kind": "interactive_user",
            "trigger": "user_message",
            "scope": {"principal": "alice"},
            "pathways": {
                "semantic": [historical, owned, public, service],
                "keyword": [historical, still_produced, public],
            },
            "results": [historical, owned, public, service],
        },
        {
            "ts": "2026-07-11T10:00:00+00:00",
            "surface": "memory_query",
            "caller_kind": "platform_service",
            "trigger": "poller",
            "scope": {"is_service": True, "is_platform_service": True},
            "pathways": {"semantic": [historical, still_produced, service]},
            "results": [historical, service],
        },
    ]
    trace = tmp_path / "trace.jsonl"
    trace.write_text("".join(json.dumps(event) + "\n" for event in events))

    report = analyze_paths(
        db=db_path, trace=trace, cutover="2026-07-01T00:00:00Z",
    )

    assert report["period"]["events"] == 2
    assert report["totals"] == {
        "events": 2,
        "pre_fusion_candidate_occurrences": 10,
        "pre_fusion_excluded_occurrences": 4,
        "pre_fusion_unique_candidates": 8,
        "pre_fusion_unique_excluded": 3,
        "post_fusion_results": 6,
        "post_fusion_excluded_results": 2,
    }
    assert report["legacy_admin_corpus"] == {"historical": 1, "post_cutover": 1}
    assert report["legacy_admin_exclusions"] == {
        "pre_fusion_historical": 1,
        "pre_fusion_post_cutover": 1,
        "post_fusion_historical": 1,
        "post_fusion_post_cutover": 0,
    }
    assert report["breakdown"][0]["surface"] == "automatic_recall"
    assert report["breakdown"][0]["post_fusion_excluded_results"] == 2
    assert report["breakdown"][1]["pre_fusion_excluded_occurrences"] == 0
    assert report["missing_trace_atom_ids"] == []


def test_legacy_acl_migration_is_copy_only_narrow_and_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    saga = SagaStore(db_path=source, embedding_dim=4)
    eligible = _atom(
        saga, "operator conversation", origin_channel="discord:operator",
        source_type="conversation",
    )
    post_cutover = _atom(
        saga, "new operator conversation", origin_channel="discord:operator",
        source_type="conversation",
    )
    service_derived = _atom(
        saga, "service output", origin_channel="discord:operator",
        source_type="service_derived",
    )
    unattributed = _atom(saga, "unattributed", source_type="conversation")
    saga.connection().executemany(
        "UPDATE atoms SET created_at = ? WHERE id = ?",
        [
            ("2026-01-01T00:00:00+00:00", eligible),
            ("2026-07-02T00:00:00+00:00", post_cutover),
            ("2026-01-01T00:00:00+00:00", service_derived),
            ("2026-01-01T00:00:00+00:00", unattributed),
        ],
    )
    saga.connection().commit()

    dry_run = migrate_copy(
        source=source,
        output=None,
        cutover="2026-07-01T00:00:00Z",
        channel_owners={"discord:operator": "alice"},
    )
    assert dry_run["eligible"] == 1
    assert dry_run["changed"] == 0
    assert saga.connection().execute(
        "SELECT owner_principal FROM atoms WHERE id = ?", (eligible,),
    ).fetchone()[0] == "legacy_admin"

    output = tmp_path / "migrated.db"
    applied = migrate_copy(
        source=source,
        output=output,
        cutover="2026-07-01T00:00:00Z",
        channel_owners={"discord:operator": "alice"},
    )
    assert applied["eligible"] == applied["changed"] == 1

    conn = sqlite3.connect(output)
    assert conn.execute(
        "SELECT owner_principal, visibility FROM atoms WHERE id = ?", (eligible,),
    ).fetchone() == ("alice", "private")
    for atom_id in (post_cutover, service_derived, unattributed):
        assert conn.execute(
            "SELECT owner_principal, visibility FROM atoms WHERE id = ?", (atom_id,),
        ).fetchone() == ("legacy_admin", "legacy_admin")

    second = migrate_operator_rows(
        conn,
        cutover="2026-07-01T00:00:00Z",
        channel_owners={"discord:operator": "alice"},
        apply=True,
    )
    assert second["eligible"] == second["changed"] == 0


def test_migration_rejects_live_in_place_output_and_reserved_owner(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    saga = SagaStore(db_path=source, embedding_dim=4)
    _atom(saga, "seed")

    try:
        migrate_copy(
            source=source,
            output=source,
            cutover="2026-07-01T00:00:00Z",
            channel_owners={"discord:operator": "alice"},
        )
    except ValueError as exc:
        assert "in-place" in str(exc)
    else:
        raise AssertionError("in-place migration was accepted")

    conn = sqlite3.connect(source)
    try:
        migrate_operator_rows(
            conn,
            cutover=datetime(2026, 7, 1, tzinfo=timezone.utc).isoformat(),
            channel_owners={"discord:operator": "legacy_admin"},
        )
    except ValueError as exc:
        assert "reserved" in str(exc)
    else:
        raise AssertionError("reserved owner mapping was accepted")
