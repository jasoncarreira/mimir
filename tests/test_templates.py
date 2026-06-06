"""Tests for mimir/templates.py — saga session-end synthesis prompt (chainlink #247).

templates.py drives every saga session-end summarization. A regression
here silently breaks the synthesis prompt — invisible until weeks later
when SAGA recall starts producing weird results. Per chainlink #247 this
was the most concerning zero-coverage module; this file pins:

- load_template: default fallback, operator override, unreadable-file fallback
- _output_preview: empty, short, long (truncation + char-count suffix), whitespace collapse
- _turn_summary_lines: empty window, single/multi turn, tool-call counting
  (typed events vs untyped-fallback), cost formatting, atom citation
- _atom_feedback_lines: empty, single atom, multi-turn citation, insertion order
- _session_has_atoms: zero-atom (storage-only) vs cited
- render_saga_session_end: lean-vs-full selection, placeholder completeness,
  operator override for both variants
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mimir.templates import (
    SAGA_SESSION_END_DEFAULT,
    SAGA_SESSION_END_LEAN_DEFAULT,
    _atom_feedback_lines,
    _output_preview,
    _session_has_atoms,
    _turn_summary_lines,
    load_template,
    render_saga_session_end,
)


# ─── load_template ──────────────────────────────────────────────────


class TestLoadTemplate:
    def test_none_prompts_dir_returns_default(self) -> None:
        assert load_template("foo", "DEFAULT", None) == "DEFAULT"

    def test_missing_file_returns_default(self, tmp_path: Path) -> None:
        assert load_template("foo", "DEFAULT", tmp_path) == "DEFAULT"

    def test_present_file_overrides_default(self, tmp_path: Path) -> None:
        (tmp_path / "foo.md").write_text("OPERATOR OVERRIDE", encoding="utf-8")
        assert load_template("foo", "DEFAULT", tmp_path) == "OPERATOR OVERRIDE"

    def test_unreadable_file_falls_back_to_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A read error (e.g. permission denied) logs + falls back rather
        than crashing the synthesis turn."""
        target = tmp_path / "foo.md"
        target.write_text("X", encoding="utf-8")

        def _boom(*_a, **_kw):
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "read_text", _boom)
        assert load_template("foo", "DEFAULT", tmp_path) == "DEFAULT"


# ─── _output_preview ────────────────────────────────────────────────


class TestOutputPreview:
    def test_empty_returns_placeholder(self) -> None:
        assert _output_preview("") == "(empty)"

    def test_short_unchanged(self) -> None:
        assert _output_preview("hello world") == "hello world"

    def test_whitespace_collapsed_to_single_line(self) -> None:
        assert _output_preview("a\n\nb   c\td") == "a b c d"

    def test_long_truncated_with_char_count(self) -> None:
        # The "(N chars total)" suffix reports len(output) — the ORIGINAL
        # length — not len(flat) after whitespace collapse. This input has
        # no whitespace so the two coincide; see
        # test_long_count_is_original_not_collapsed_length for the case
        # where they differ.
        text = "x" * 500
        result = _output_preview(text)
        assert result.startswith("x" * 200)
        assert "(500 chars total)" in result
        assert "…" in result

    def test_long_count_is_original_not_collapsed_length(self) -> None:
        """The char-count suffix reports the original output length, not the
        whitespace-collapsed preview length — so an agent reading the
        preview knows the true size of what mimir_get_turn would return."""
        # 300 "x" + 300 spaces = 600 original chars; collapse drops the
        # run of spaces, but the suffix must still say 600.
        text = ("x" * 300) + (" " * 300)
        result = _output_preview(text)
        assert "(600 chars total)" in result

    def test_exactly_at_cap_not_truncated(self) -> None:
        text = "y" * 200
        result = _output_preview(text)
        assert result == text
        assert "chars total" not in result


# ─── _turn_summary_lines ────────────────────────────────────────────


class TestTurnSummaryLines:
    def test_empty_window(self) -> None:
        assert _turn_summary_lines([]) == "(no turns recorded for this session)"

    def test_single_turn_shape(self) -> None:
        turns = [{
            "turn_id": "abc123",
            "trigger": "user_message",
            "total_cost_usd": 0.0123,
            "events": [{"type": "tool_call", "name": "Read"}],
            "saga_atom_ids": ["atom-1"],
            "output": "did a thing",
        }]
        out = _turn_summary_lines(turns)
        assert "turn abc123" in out
        assert "user_message" in out
        assert "$0.012" in out
        assert "1 tool calls" in out
        assert "atoms: atom-1" in out
        assert "output: did a thing" in out

    def test_unknown_cost_renders_question_mark(self) -> None:
        turns = [{"turn_id": "t", "trigger": "x", "events": [], "output": ""}]
        out = _turn_summary_lines(turns)
        assert "$?" in out

    def test_zero_cost_renders_dollar_zero_not_question_mark(self) -> None:
        """0.0 is falsy but a real cost — the source uses
        ``isinstance(cost, (int, float))`` rather than a truthy check, so a
        genuinely-free turn must render ``$0.000``, not ``$?``. Pins the
        not-falsy-check (the footgun the isinstance guard avoids)."""
        turns = [{
            "turn_id": "t", "trigger": "x", "total_cost_usd": 0.0,
            "events": [], "output": "o",
        }]
        out = _turn_summary_lines(turns)
        assert "$0.000" in out
        assert "$?" not in out

    def test_no_atoms_renders_none(self) -> None:
        turns = [{"turn_id": "t", "trigger": "x", "events": [], "output": "o"}]
        out = _turn_summary_lines(turns)
        assert "atoms: (none)" in out

    def test_tool_call_count_uses_typed_events(self) -> None:
        turns = [{
            "turn_id": "t", "trigger": "x",
            "events": [
                {"type": "tool_call", "name": "Read"},
                {"type": "tool_call", "name": "Write"},
                {"type": "text", "content": "..."},  # not a tool call
            ],
            "output": "o",
        }]
        out = _turn_summary_lines(turns)
        assert "2 tool calls" in out

    def test_tool_call_count_fallback_for_untyped_events(self) -> None:
        """Older fixtures whose events have no `type` key fall back to the
        raw event count — pins the back-compat branch."""
        turns = [{
            "turn_id": "t", "trigger": "x",
            "events": [{"foo": 1}, {"bar": 2}, {"baz": 3}],
            "output": "o",
        }]
        out = _turn_summary_lines(turns)
        assert "3 tool calls" in out

    def test_multi_turn_one_line_each(self) -> None:
        turns = [
            {"turn_id": "t1", "trigger": "a", "events": [], "output": "o1"},
            {"turn_id": "t2", "trigger": "b", "events": [], "output": "o2"},
        ]
        out = _turn_summary_lines(turns)
        assert "turn t1" in out
        assert "turn t2" in out

    def test_injected_inputs_surfaced(self) -> None:
        """chainlink #376: mid-turn folded messages appear in the
        synthesis-visible summary so session-end synthesis sees them."""
        turns = [{
            "turn_id": "t", "trigger": "user_message",
            "events": [], "output": "ok",
            "injected_inputs": [
                "[mid-turn message from alice]\nalso check staging",
                "[mid-turn message from alice]\nand prod",
            ],
        }]
        out = _turn_summary_lines(turns)
        assert "injected mid-turn (2)" in out
        assert "also check staging" in out
        assert "and prod" in out

    def test_no_injected_line_when_absent(self) -> None:
        turns = [{"turn_id": "t", "trigger": "x", "events": [], "output": "o"}]
        out = _turn_summary_lines(turns)
        assert "injected mid-turn" not in out


# ─── _atom_feedback_lines ───────────────────────────────────────────


class TestAtomFeedbackLines:
    def test_empty_window(self) -> None:
        assert _atom_feedback_lines([]) == "(no atoms cited in this session)"

    def test_no_atoms_cited(self) -> None:
        turns = [{"turn_id": "t", "saga_atom_ids": []}]
        assert _atom_feedback_lines(turns) == "(no atoms cited in this session)"

    def test_single_atom_single_turn(self) -> None:
        turns = [{"turn_id": "t1", "saga_atom_ids": ["atom-A"]}]
        out = _atom_feedback_lines(turns)
        assert "atom-A: cited in turn(s) t1" in out

    def test_atom_cited_in_multiple_turns(self) -> None:
        turns = [
            {"turn_id": "t1", "saga_atom_ids": ["atom-A"]},
            {"turn_id": "t2", "saga_atom_ids": ["atom-A"]},
        ]
        out = _atom_feedback_lines(turns)
        assert "atom-A: cited in turn(s) t1, t2" in out

    def test_insertion_order_preserved(self) -> None:
        """Stable order across re-renders: atoms appear in first-citing-turn
        order, not sorted."""
        turns = [
            {"turn_id": "t1", "saga_atom_ids": ["zzz", "aaa"]},
            {"turn_id": "t2", "saga_atom_ids": ["mmm"]},
        ]
        out = _atom_feedback_lines(turns)
        lines = out.split("\n")
        assert lines[0].startswith("- zzz")
        assert lines[1].startswith("- aaa")
        assert lines[2].startswith("- mmm")


# ─── _session_has_atoms ─────────────────────────────────────────────


class TestSessionHasAtoms:
    def test_empty_window_false(self) -> None:
        assert _session_has_atoms([]) is False

    def test_storage_only_session_false(self) -> None:
        """A turn that stored atoms but cited none registers as zero —
        lean path fires (chainlink #7 semantics)."""
        turns = [{"turn_id": "t", "saga_atom_ids": []}]
        assert _session_has_atoms(turns) is False

    def test_cited_atoms_true(self) -> None:
        turns = [
            {"turn_id": "t1", "saga_atom_ids": []},
            {"turn_id": "t2", "saga_atom_ids": ["atom-X"]},
        ]
        assert _session_has_atoms(turns) is True


# ─── render_saga_session_end ────────────────────────────────────────


class TestRenderSagaSessionEnd:
    def _turns_with_atoms(self) -> list[dict]:
        return [{
            "turn_id": "t1", "trigger": "user_message",
            "total_cost_usd": 0.05,
            "events": [{"type": "tool_call", "name": "saga_query"}],
            "saga_atom_ids": ["atom-1"],
            "output": "answered",
        }]

    def _turns_no_atoms(self) -> list[dict]:
        return [{
            "turn_id": "t1", "trigger": "scheduled_tick",
            "events": [], "saga_atom_ids": [], "output": "tick",
        }]

    def test_full_variant_when_atoms_cited(self) -> None:
        out = render_saga_session_end(
            channel_id="chan-1",
            saga_session_id="saga-1",
            idle_minutes=10,
            turns_window=self._turns_with_atoms(),
            prompts_dir=None,
        )
        # Full template carries the atoms-cited block.
        assert "chan-1" in out
        assert "saga-1" in out
        assert "10" in out
        assert "atom-1" in out
        # No unrendered placeholders.
        assert "{channel_id}" not in out
        assert "{atom_feedback_block}" not in out
        assert "{turn_summary_block}" not in out

    def test_lean_variant_when_no_atoms(self) -> None:
        out = render_saga_session_end(
            channel_id="chan-2",
            saga_session_id="saga-2",
            idle_minutes=5,
            turns_window=self._turns_no_atoms(),
            prompts_dir=None,
        )
        assert "chan-2" in out
        assert "saga-2" in out
        # Lean template has no atom_feedback_block slot — and it must not
        # leak an unrendered placeholder.
        assert "{atom_feedback_block}" not in out
        assert "{turn_summary_block}" not in out

    def test_operator_override_full_variant(self, tmp_path: Path) -> None:
        (tmp_path / "saga_session_end.md").write_text(
            "OVERRIDE chan={channel_id} atoms={atom_feedback_block}",
            encoding="utf-8",
        )
        out = render_saga_session_end(
            channel_id="c", saga_session_id="s", idle_minutes=1,
            turns_window=self._turns_with_atoms(),
            prompts_dir=tmp_path,
        )
        assert out.startswith("OVERRIDE chan=c atoms=")
        assert "atom-1" in out

    def test_operator_override_lean_variant(self, tmp_path: Path) -> None:
        (tmp_path / "saga_session_end_lean.md").write_text(
            "LEAN OVERRIDE chan={channel_id} turns={turn_summary_block}",
            encoding="utf-8",
        )
        out = render_saga_session_end(
            channel_id="c", saga_session_id="s", idle_minutes=1,
            turns_window=self._turns_no_atoms(),
            prompts_dir=tmp_path,
        )
        assert out.startswith("LEAN OVERRIDE chan=c turns=")

    def test_default_templates_have_matching_placeholders(self) -> None:
        """Guard: the bundled defaults must only reference placeholders
        render_saga_session_end supplies. A typo'd ``{atom_feedback_block}``
        in the lean default (which gets no such kwarg) would KeyError every
        no-atom session — exactly the silent-breakage class this chainlink
        was filed for."""
        # Full default may use atom_feedback_block; lean must NOT.
        assert "{atom_feedback_block}" in SAGA_SESSION_END_DEFAULT
        assert "{atom_feedback_block}" not in SAGA_SESSION_END_LEAN_DEFAULT
        # Both share these.
        for tmpl in (SAGA_SESSION_END_DEFAULT, SAGA_SESSION_END_LEAN_DEFAULT):
            assert "{channel_id}" in tmpl
            assert "{saga_session_id}" in tmpl
            assert "{idle_minutes}" in tmpl
            assert "{turn_summary_block}" in tmpl
