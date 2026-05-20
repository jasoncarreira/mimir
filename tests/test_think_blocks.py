"""Tests for ``mimir._think_blocks`` — Minimax / DeepSeek-R1 / QwQ
style ``<think>...</think>`` reasoning extraction.

The util has three jobs:

* Pull closed blocks out into a separate list (preserve order).
* Capture an unclosed trailing ``<think>...EOF`` block (model hit
  max_tokens mid-reasoning) the same way.
* Return whatever visible text remains, trimmed.

Fast-path: input with no ``<think>`` returns identity tuple cheaply.
"""
from __future__ import annotations

from mimir._think_blocks import extract_think_blocks, strip_think_blocks


def test_no_tags_returns_input_unchanged():
    text = "just a normal reply, no reasoning blocks"
    visible, blocks = extract_think_blocks(text)
    assert visible == text
    assert blocks == []


def test_empty_input():
    visible, blocks = extract_think_blocks("")
    assert visible == ""
    assert blocks == []


def test_closed_block_extracted():
    text = "<think>let me think about this</think>The answer is 42."
    visible, blocks = extract_think_blocks(text)
    assert visible == "The answer is 42."
    assert blocks == ["let me think about this"]


def test_multiple_closed_blocks_preserve_order():
    """Some inference paths interleave reasoning between paragraphs;
    each block becomes its own list entry, top-to-bottom."""
    text = (
        "<think>first reason</think>"
        "paragraph one. "
        "<think>second reason</think>"
        "paragraph two."
    )
    visible, blocks = extract_think_blocks(text)
    assert visible == "paragraph one. paragraph two."
    assert blocks == ["first reason", "second reason"]


def test_unclosed_trailing_block_captured_as_reasoning():
    """Model hit max_tokens mid-reasoning. Everything after the open
    ``<think>`` tag is reasoning, NOT a partial visible reply."""
    text = "<think>The user wants a reply that is exactly:"
    visible, blocks = extract_think_blocks(text)
    assert visible == ""
    assert blocks == ["The user wants a reply that is exactly:"]


def test_unclosed_trailing_block_with_prior_visible_text():
    """Visible text BEFORE the trailing-open is still a real reply
    (the model decided + then started reasoning about the next thing
    when it got cut off)."""
    text = (
        "Sure, here is your answer: 42.\n"
        "<think>Now let me explain why"
    )
    visible, blocks = extract_think_blocks(text)
    assert visible == "Sure, here is your answer: 42."
    assert blocks == ["Now let me explain why"]


def test_unclosed_trailing_after_closed_block():
    """Mixed: one closed reasoning block, then a real reply, then
    a truncated open block. Both reasoning entries land in the list."""
    text = (
        "<think>initial reasoning</think>"
        "Sure, here it is: 42.\n"
        "<think>now reasoning about followup"
    )
    visible, blocks = extract_think_blocks(text)
    assert visible == "Sure, here it is: 42."
    assert blocks == ["initial reasoning", "now reasoning about followup"]


def test_multiline_block_with_dotall():
    text = "<think>line one\nline two\nline three</think>final."
    visible, blocks = extract_think_blocks(text)
    assert visible == "final."
    assert blocks == ["line one\nline two\nline three"]


def test_whitespace_inside_think_block_is_trimmed():
    """The extracted reasoning is trimmed at its edges so a thin
    leading/trailing newline (from the model's formatting) doesn't
    propagate into the captured block."""
    text = "<think>\n  reasoning here  \n</think>reply"
    visible, blocks = extract_think_blocks(text)
    assert blocks == ["reasoning here"]
    assert visible == "reply"


def test_strip_think_blocks_drops_reasoning():
    """The convenience wrapper returns just visible text — for
    callers (``_record_outbound``, contextual-rewrite context) that
    don't need to retain the reasoning."""
    text = "<think>private scratchpad</think>The answer is 42."
    assert strip_think_blocks(text) == "The answer is 42."


def test_strip_think_blocks_returns_empty_when_only_reasoning():
    """If the model produced ONLY a reasoning block (cut off before
    emitting visible text), the stripped form is empty — callers
    must guard against this. ``_record_outbound`` does (skips the
    buffer.append if content becomes empty post-strip)."""
    text = "<think>thinking but never finished</think>"
    assert strip_think_blocks(text) == ""


def test_fast_path_no_tag_substring():
    """Even with malformed-looking input that has neither ``<think>``
    open tag nor any tag-like marker, the cheap path returns input
    unchanged without invoking the regex."""
    text = "this < has angle brackets but no think tag >"
    visible, blocks = extract_think_blocks(text)
    assert visible == text
    assert blocks == []
