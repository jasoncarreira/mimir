"""MSAM Contradictions Tests -- unit tests for semantic contradiction detection."""

import os
import sys
import json
import struct
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

import pytest

# Ensure msam is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from saga.contradictions import (
    _detect_negation,
    _detect_antonyms,
    _detect_value_conflict,
    _detect_temporal_supersession,
)


class TestDetectNegation:
    def test_negation_detected(self):
        assert _detect_negation(
            "Alice enjoys running every morning",
            "Alice does not enjoy running every morning",
        )

    def test_no_negation_when_both_positive(self):
        assert not _detect_negation(
            "Alice enjoys running every morning",
            "Alice likes running every morning",
        )

    def test_no_negation_when_both_negative(self):
        assert not _detect_negation(
            "Alice doesn't enjoy running",
            "Alice never liked running",
        )

    def test_no_negation_unrelated_texts(self):
        assert not _detect_negation(
            "The weather is sunny",
            "I bought new shoes",
        )


class TestDetectTemporalSupersession:
    def test_different_dates_in_content(self):
        atom_a = {"content": "As of 2024-01-15, the price is $100", "created_at": "2024-01-15T00:00:00+00:00"}
        atom_b = {"content": "As of 2024-06-01, the price is $120", "created_at": "2024-06-01T00:00:00+00:00"}
        assert _detect_temporal_supersession(atom_a, atom_b)

    def test_temporal_words_one_side(self):
        atom_a = {"content": "Alice currently lives in NYC", "created_at": "2024-06-01T00:00:00+00:00"}
        atom_b = {"content": "Alice lives in Boston", "created_at": "2024-01-01T00:00:00+00:00"}
        assert _detect_temporal_supersession(atom_a, atom_b)

    def test_no_temporal_supersession_same_date(self):
        atom_a = {"content": "Price is $100", "created_at": "2024-01-15T00:00:00+00:00"}
        atom_b = {"content": "Quality is high", "created_at": "2024-01-15T00:00:00+00:00"}
        assert not _detect_temporal_supersession(atom_a, atom_b)


class TestDetectValueConflict:
    def test_different_values_same_property(self):
        assert _detect_value_conflict(
            "Alice lives in New York City",
            "Alice lives in Los Angeles",
        )

    def test_same_value_no_conflict(self):
        assert not _detect_value_conflict(
            "Alice lives in New York City",
            "Alice lives in New York City",
        )

    def test_no_conflict_different_properties(self):
        # Texts with no shared verb patterns should not conflict
        assert not _detect_value_conflict(
            "The weather looks nice today",
            "I enjoy reading books at home",
        )


class TestDetectAntonyms:
    def test_antonym_pair_detected(self):
        assert _detect_antonyms(
            "I love this project",
            "I hate this project",
        )

    def test_no_antonyms(self):
        assert not _detect_antonyms(
            "The cat sat on the mat",
            "The dog played in the yard",
        )

    def test_start_stop_antonyms(self):
        assert _detect_antonyms(
            "We should start the migration",
            "We should stop the migration",
        )

    def test_accept_reject_antonyms(self):
        assert _detect_antonyms(
            "The committee will accept the proposal",
            "The committee will reject the proposal",
        )


class TestCheckBeforeStore:
    @patch("saga.contradictions.get_db")
    @patch("saga.contradictions.embed_query")
    def test_returns_empty_for_no_atoms(self, mock_embed, mock_db):
        mock_embed.return_value = [0.0] * 1024
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = []
        mock_db.return_value = mock_conn

        from saga.contradictions import check_before_store
        result = check_before_store("some new content")
        assert result == []

    @patch("saga.contradictions.get_db")
    @patch("saga.contradictions.embed_query")
    @patch("saga.contradictions.unpack_embedding")
    @patch("saga.contradictions.cosine_similarity")
    def test_returns_empty_for_non_contradictory(self, mock_sim, mock_unpack, mock_embed, mock_db):
        from datetime import datetime, timezone
        mock_embed.return_value = [1.0] * 1024
        mock_unpack.return_value = [1.0] * 1024
        # Low similarity means no contradiction check triggered
        mock_sim.return_value = 0.50

        # Use current time so temporal supersession doesn't trigger
        now_iso = datetime.now(timezone.utc).isoformat()
        fake_row = {
            "id": "abc123",
            "content": "The sky is blue",
            "embedding": b"\x00" * 4096,
            "topics": "[]",
            "created_at": now_iso,
            "arousal": 0.5,
            "valence": 0.0,
        }
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [fake_row]
        mock_db.return_value = mock_conn

        from saga.contradictions import check_before_store
        # Use identical content so no detector triggers
        result = check_before_store("The sky is blue")
        assert result == []


class TestFindSemanticContradictions:
    @patch("saga.contradictions.get_db")
    def test_returns_empty_for_no_atoms(self, mock_db):
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = []
        mock_db.return_value = mock_conn

        from saga.contradictions import find_semantic_contradictions
        result = find_semantic_contradictions()
        assert result == []
        assert isinstance(result, list)


class TestResolveContradictionsToSupersedes:
    """P4-bench: resolver picks newer atom and writes supersedes edges."""

    def test_skips_antonyms(self, monkeypatch):
        """Antonym contradictions are too noisy — should not produce edges."""
        from saga import core

        fake_pair = [{
            "atom_a": {"id": "a", "content": "user is happy", "created_at": "2026-01-02T00:00:00+00:00"},
            "atom_b": {"id": "b", "content": "user is sad", "created_at": "2026-01-01T00:00:00+00:00"},
            "similarity": 0.9,
            "contradiction_type": "semantic_opposition",
            "suggestion": "...",
        }]

        called = []
        monkeypatch.setattr("saga.contradictions.find_semantic_contradictions",
                            lambda threshold=0.85: fake_pair)
        monkeypatch.setattr(core, "add_atom_relation",
                            lambda *a, **k: called.append((a, k)))

        result = core.resolve_contradictions_to_supersedes(raws_only=False)
        assert result["contradictions_found"] == 1
        assert result["supersedes_written"] == 0
        assert called == []

    def test_writes_edge_for_value_conflict_newer_wins(self, monkeypatch):
        from saga import core

        # b is newer; b should supersede a.
        fake_pair = [{
            "atom_a": {"id": "a", "content": "user works at Acme",
                       "created_at": "2026-01-01T00:00:00+00:00"},
            "atom_b": {"id": "b", "content": "user works at Beta",
                       "created_at": "2026-03-01T00:00:00+00:00"},
            "similarity": 0.9,
            "contradiction_type": "value_conflict",
            "suggestion": "...",
        }]

        captured = []
        monkeypatch.setattr("saga.contradictions.find_semantic_contradictions",
                            lambda threshold=0.85: fake_pair)
        monkeypatch.setattr(core, "add_atom_relation",
                            lambda src, tgt, rt, **k: captured.append((src, tgt, rt, k)))

        result = core.resolve_contradictions_to_supersedes(raws_only=False)
        assert result["supersedes_written"] == 1
        src, tgt, rt, kwargs = captured[0]
        assert src == "b"  # newer
        assert tgt == "a"  # older
        assert rt == "supersedes"
        assert kwargs["confidence"] == 0.9
        assert kwargs["metadata"]["contradiction_type"] == "value_conflict"

    def test_skips_when_timestamps_equal(self, monkeypatch):
        from saga import core

        same_ts = "2026-01-01T00:00:00+00:00"
        fake_pair = [{
            "atom_a": {"id": "a", "content": "x", "created_at": same_ts},
            "atom_b": {"id": "b", "content": "y", "created_at": same_ts},
            "similarity": 0.9,
            "contradiction_type": "negation",
            "suggestion": "...",
        }]

        captured = []
        monkeypatch.setattr("saga.contradictions.find_semantic_contradictions",
                            lambda threshold=0.85: fake_pair)
        monkeypatch.setattr(core, "add_atom_relation",
                            lambda *a, **k: captured.append((a, k)))

        result = core.resolve_contradictions_to_supersedes(raws_only=False)
        assert result["supersedes_written"] == 0
        assert captured == []


class TestSupersedesDemotionInRetrieval:
    """P4-bench: hybrid_retrieve demotes superseded atoms in the candidate pool."""

    def test_apply_supersedes_demotion_no_op_on_empty(self):
        from saga.core import _apply_supersedes_demotion
        # Just verify the function tolerates empty input.
        d = {}
        _apply_supersedes_demotion(d, demotion_factor=0.4)
        assert d == {}

    def test_apply_supersedes_demotion_skips_when_factor_one(self):
        from saga.core import _apply_supersedes_demotion
        d = {"a": {"_combined_score": 1.0}}
        _apply_supersedes_demotion(d, demotion_factor=1.0)
        assert d["a"]["_combined_score"] == 1.0  # untouched


class TestResolveSupersedesForNewAtom:
    """P4-bench prod write-time path: store_atom hook calls
    _resolve_supersedes_for_new_atom which uses check_before_store."""

    def test_writes_edge_for_value_conflict(self, monkeypatch):
        from saga import core

        # Simulate one contradiction returned from check_before_store.
        fake = [{
            "atom_a": {"id": "__pending__", "content": "user works at Beta",
                       "created_at": "2026-04-26T00:00:00+00:00"},
            "atom_b": {"id": "existing-id", "content": "user works at Acme",
                       "created_at": "2026-01-01T00:00:00+00:00"},
            "similarity": 0.9,
            "contradiction_type": "value_conflict",
            "suggestion": "...",
        }]

        monkeypatch.setattr("saga.contradictions.check_before_store",
                            lambda content, top_k=5: fake)
        captured = []
        monkeypatch.setattr(core, "add_atom_relation",
                            lambda src, tgt, rt, **k: captured.append((src, tgt, rt, k)))

        n = core._resolve_supersedes_for_new_atom("new-id", "user works at Beta")
        assert n == 1
        src, tgt, rt, kwargs = captured[0]
        assert src == "new-id"
        assert tgt == "existing-id"
        assert rt == "supersedes"
        assert kwargs["metadata"]["trigger"] == "store_atom"

    def test_skips_antonym_contradiction(self, monkeypatch):
        from saga import core
        fake = [{
            "atom_a": {"id": "__pending__", "content": "user is happy",
                       "created_at": "2026-04-26T00:00:00+00:00"},
            "atom_b": {"id": "existing-id", "content": "user is sad",
                       "created_at": "2026-01-01T00:00:00+00:00"},
            "similarity": 0.9,
            "contradiction_type": "semantic_opposition",
            "suggestion": "...",
        }]
        monkeypatch.setattr("saga.contradictions.check_before_store",
                            lambda content, top_k=5: fake)
        captured = []
        monkeypatch.setattr(core, "add_atom_relation",
                            lambda *a, **k: captured.append((a, k)))

        n = core._resolve_supersedes_for_new_atom("new-id", "user is happy")
        assert n == 0
        assert captured == []

    def test_skips_below_threshold(self, monkeypatch):
        from saga import core
        fake = [{
            "atom_a": {"id": "__pending__", "content": "x",
                       "created_at": "2026-04-26T00:00:00+00:00"},
            "atom_b": {"id": "existing-id", "content": "y",
                       "created_at": "2026-01-01T00:00:00+00:00"},
            "similarity": 0.5,  # below 0.85
            "contradiction_type": "value_conflict",
            "suggestion": "...",
        }]
        monkeypatch.setattr("saga.contradictions.check_before_store",
                            lambda content, top_k=5: fake)
        captured = []
        monkeypatch.setattr(core, "add_atom_relation",
                            lambda *a, **k: captured.append((a, k)))

        n = core._resolve_supersedes_for_new_atom("new-id", "x", threshold=0.85)
        assert n == 0
        assert captured == []
