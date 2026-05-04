"""Tests for mimir.bridges._directives."""

from __future__ import annotations

from mimir.bridges._directives import (
    ReactDirective,
    SendFileDirective,
    has_incomplete_actions_tag,
    has_unclosed_actions_block,
    parse_directives,
    strip_actions_blocks,
)


def test_no_actions_block_passes_through():
    result = parse_directives("just regular text, no actions here")
    assert result.clean_text == "just regular text, no actions here"
    assert result.directives == ()


def test_simple_react_directive():
    result = parse_directives(
        'Got it.\n<actions><react emoji="thumbsup" /></actions>'
    )
    assert result.clean_text == "Got it."
    assert len(result.directives) == 1
    d = result.directives[0]
    assert isinstance(d, ReactDirective)
    assert d.emoji == "thumbsup"
    assert d.message_id is None


def test_react_with_message_id():
    result = parse_directives(
        '<actions><react emoji=":eyes:" message="123456789" /></actions>'
    )
    d = result.directives[0]
    assert isinstance(d, ReactDirective)
    assert d.emoji == ":eyes:"
    assert d.message_id == "123456789"


def test_react_channel_attribute_is_ignored():
    """``channel`` on directives is no longer supported — the parent
    send_message's channel is the only target. The attribute is
    silently ignored on parse (no error, no special handling)."""
    result = parse_directives(
        '<actions><react emoji="fire" channel="987" message="123" /></actions>'
    )
    d = result.directives[0]
    assert isinstance(d, ReactDirective)
    assert d.message_id == "123"
    # ReactDirective no longer has a channel_id field.
    assert not hasattr(d, "channel_id")


def test_send_file_directive():
    result = parse_directives(
        'Here it is.\n<actions><send-file path="report.pdf" caption="Q3" /></actions>'
    )
    assert result.clean_text == "Here it is."
    d = result.directives[0]
    assert isinstance(d, SendFileDirective)
    assert d.path == "report.pdf"
    assert d.caption == "Q3"
    assert d.kind is None
    assert d.cleanup is False


def test_send_file_with_kind_and_cleanup():
    result = parse_directives(
        '<actions><send-file path="chart.png" kind="image" cleanup="true" /></actions>'
    )
    d = result.directives[0]
    assert isinstance(d, SendFileDirective)
    assert d.kind == "image"
    assert d.cleanup is True


def test_send_message_directive_no_longer_supported():
    """<send-message> was removed; it produces no directives. The text
    inside the tag stays in clean_text (the tag itself isn't recognized,
    so it's left as-is — the agent should just call send_message
    directly with an explicit channel_id for cross-channel sends)."""
    result = parse_directives(
        '<actions><send-message channel="discord-987">heads up</send-message></actions>'
    )
    assert result.directives == ()


def test_multiple_directives_preserve_order():
    result = parse_directives(
        'reply.\n<actions>'
        '<react emoji="eyes" />'
        '<send-file path="a.png" />'
        '<react emoji="fire" />'
        '</actions>'
    )
    assert len(result.directives) == 3
    assert isinstance(result.directives[0], ReactDirective)
    assert result.directives[0].emoji == "eyes"
    assert isinstance(result.directives[1], SendFileDirective)
    assert isinstance(result.directives[2], ReactDirective)
    assert result.directives[2].emoji == "fire"


def test_text_around_block_preserved():
    result = parse_directives(
        "before\n\n<actions><react emoji=\"x\" /></actions>\n\nafter"
    )
    assert result.clean_text == "before\n\nafter"


def test_multiple_blocks_concatenated():
    result = parse_directives(
        "one <actions><react emoji=\"a\" /></actions> two "
        "<actions><react emoji=\"b\" /></actions> three"
    )
    assert "one" in result.clean_text and "two" in result.clean_text
    assert "three" in result.clean_text
    assert len(result.directives) == 2
    assert result.directives[0].emoji == "a"  # type: ignore[attr-defined]
    assert result.directives[1].emoji == "b"  # type: ignore[attr-defined]


def test_react_without_emoji_is_skipped():
    result = parse_directives('<actions><react /></actions>')
    assert result.directives == ()


def test_send_file_without_path_is_skipped():
    result = parse_directives('<actions><send-file /></actions>')
    assert result.directives == ()


def test_send_file_accepts_file_attr_alias():
    """``file=`` is an accepted alias for ``path=`` (lettabot compat)."""
    result = parse_directives('<actions><send-file file="x.txt" /></actions>')
    assert len(result.directives) == 1
    assert result.directives[0].path == "x.txt"  # type: ignore[attr-defined]


def test_send_file_channel_attribute_is_ignored():
    """``channel`` on send-file is no longer supported — directives
    target the parent send_message's channel only."""
    result = parse_directives(
        '<actions><send-file path="x.txt" channel="other-1" /></actions>'
    )
    d = result.directives[0]
    assert isinstance(d, SendFileDirective)
    assert d.path == "x.txt"
    assert not hasattr(d, "channel_id")


def test_unknown_tags_inside_block_are_ignored():
    """Unknown directive tags don't break the parser — known ones still
    parse and the unknown is silently dropped."""
    result = parse_directives(
        '<actions>'
        '<react emoji="ok" />'
        '<unknown-tag attr="x" />'
        '</actions>'
    )
    assert len(result.directives) == 1
    assert result.directives[0].emoji == "ok"  # type: ignore[attr-defined]


def test_attributes_tolerate_single_quotes():
    result = parse_directives(
        "<actions><react emoji='thumbsup' /></actions>"
    )
    assert result.directives[0].emoji == "thumbsup"  # type: ignore[attr-defined]


def test_self_closing_with_extra_whitespace():
    """LLMs sometimes put space before ``/>``; tolerate it."""
    result = parse_directives(
        '<actions><react emoji="eyes"  /></actions>'
    )
    assert len(result.directives) == 1


def test_unclosed_block_returns_text_unchanged():
    """Unmatched <actions> with no close → no directives parsed; text
    returned with the unclosed marker still present (the streaming
    helpers detect this case for display)."""
    raw = "starting up <actions><react emoji=\"x\" "
    result = parse_directives(raw)
    assert result.directives == ()
    assert "<actions>" in result.clean_text


def test_strip_actions_blocks_only():
    """``strip_actions_blocks`` produces just the cleaned text, no
    directives — for transcript mirroring / logging paths."""
    cleaned = strip_actions_blocks(
        "hello\n<actions><react emoji=\"x\" /></actions>\n"
    )
    assert cleaned == "hello"


# ─── Streaming helpers ──────────────────────────────────────────────


def test_has_unclosed_actions_block_open_then_close():
    assert has_unclosed_actions_block("<actions>") is True
    assert has_unclosed_actions_block("<actions></actions>") is False
    assert has_unclosed_actions_block("none here") is False


def test_has_unclosed_actions_block_after_full_then_partial():
    assert has_unclosed_actions_block(
        "<actions></actions> more text <actions>"
    ) is True


def test_has_incomplete_actions_tag_partial_open():
    assert has_incomplete_actions_tag("regular text <act") is True
    assert has_incomplete_actions_tag("regular text <actions>") is False


def test_has_incomplete_actions_tag_partial_close():
    # The agent has emitted ``</a`` but not finished the close yet —
    # caller should hold the next chunk to avoid splitting the tag.
    assert has_incomplete_actions_tag("body <actions>x</a") is True


def test_has_incomplete_actions_tag_no_partial():
    # Last `<` is followed by `>` — no partial tag pending.
    assert has_incomplete_actions_tag("body <p>text</p>") is False
