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
    """Multiple action blocks each on their own line are concatenated.
    Inline (mid-line) blocks are NOT parsed under the line-anchored
    convention — see ``test_inline_mention_does_not_parse`` for why."""
    result = parse_directives(
        "one\n<actions><react emoji=\"a\" /></actions>\n two\n"
        "<actions><react emoji=\"b\" /></actions>\n three"
    )
    assert "one" in result.clean_text and "two" in result.clean_text
    assert "three" in result.clean_text
    assert len(result.directives) == 2
    assert result.directives[0].emoji == "a"  # type: ignore[attr-defined]
    assert result.directives[1].emoji == "b"  # type: ignore[attr-defined]


def test_inline_mention_does_not_parse():
    """Regression: mentions of the directive tag inside prose (e.g. in
    inline code formatting) must NOT be paired with a real trailing
    block, because that greedily strips all the prose between them.

    Pre-fix, this text would have its middle section deleted: the parser
    saw a stray inline-code mention as the open tag, paired it with the
    real trailing close, and the cleaned text became just the prefix.
    Post-fix the inline mention is on a non-anchored line and is not
    matched."""
    raw = (
        "Found it: when the agent writes the literal `<actions>` tag in "
        "code-quoted prose AND then emits a real trailing block on its "
        "own line, the parser used to greedily eat everything between "
        "them.\n"
        "\n"
        "<actions><react emoji=\"thumbsup\" /></actions>"
    )
    result = parse_directives(raw)
    # Prose body should survive intact (no truncation at the inline mention).
    assert "Found it" in result.clean_text
    assert "code-quoted prose" in result.clean_text
    assert "trailing block" in result.clean_text
    # Real trailing block parsed exactly once.
    assert len(result.directives) == 1
    assert result.directives[0].emoji == "thumbsup"  # type: ignore[attr-defined]


def test_block_with_indented_open_tag():
    """Markdown often indents nested content; the open tag may have
    leading whitespace (spaces or tabs) and still count as line-anchored."""
    result = parse_directives(
        "context:\n"
        "  <actions>\n"
        "    <react emoji=\"eyes\" />\n"
        "  </actions>"
    )
    assert len(result.directives) == 1
    assert result.directives[0].emoji == "eyes"  # type: ignore[attr-defined]


def test_inline_mention_without_trailing_block():
    """A bare inline mention with no trailing block just stays in the
    cleaned text — same as the no-actions-here passthrough."""
    raw = "I'd write `<actions>` to react; here's the explanation."
    result = parse_directives(raw)
    assert result.directives == ()
    assert result.clean_text == raw


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


# ─── send_message tool directive dispatch ───────────────────────────


from dataclasses import dataclass, field

import pytest

from mimir.bridges.base import Bridge, SendResult
from mimir.channel_registry import ChannelRegistry
from mimir.tools.registry import (
    reset_current_channel_id,
    send_message,
    set_channel_registry,
    set_current_channel_id,
)


@dataclass
class _CaptureBridge(Bridge):
    name: str = "cap"
    prefixes: tuple = ("cap-",)
    sent: list[tuple[str, str]] = field(default_factory=list)
    reacted: list[tuple] = field(default_factory=list)
    _last_id: str = "msg-001"

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...

    async def send(self, channel_id, text, attachment_paths=None, *, final=True):
        self.sent.append((channel_id, text))
        return SendResult(sent=True, message_id=self._last_id, chunks=1)

    async def react(self, channel_id, message_id, emoji):
        self.reacted.append((channel_id, message_id, emoji))
        return True


def _make_registry(bridge: _CaptureBridge) -> ChannelRegistry:
    reg = ChannelRegistry()
    reg.register(bridge)
    return reg


@pytest.mark.asyncio
async def test_send_message_strips_actions_and_reacts():
    """send_message strips <actions> block from outbound text and calls
    bridge.react() for each ReactDirective."""
    bridge = _CaptureBridge()
    set_channel_registry(_make_registry(bridge))
    cid_token = set_current_channel_id("cap-test")
    try:
        result = await send_message.ainvoke(
            {
                "text": (
                    'Done.\n\n<actions>'
                    '<react emoji="✅" message="999" />'
                    '</actions>'
                )
            }
        )

        # Only clean text went to Discord — no <actions> in the sent body.
        assert len(bridge.sent) == 1
        assert bridge.sent[0][1] == "Done."

        # React was dispatched with the explicit message_id.
        assert len(bridge.reacted) == 1
        assert bridge.reacted[0] == ("cap-test", "999", "✅")

        assert "send_message ok" in result
    finally:
        reset_current_channel_id(cid_token)
        set_channel_registry(None)


@pytest.mark.asyncio
async def test_send_message_react_defaults_to_sent_message_id():
    """When <react> has no message attribute, react targets the just-sent
    message (bridge.send's returned message_id)."""
    bridge = _CaptureBridge()
    bridge._last_id = "auto-id-42"
    set_channel_registry(_make_registry(bridge))
    cid_token = set_current_channel_id("cap-test")
    try:
        await send_message.ainvoke(
            {
                "text": (
                    'ACK.\n\n<actions>'
                    '<react emoji="👍" />'
                    '</actions>'
                )
            }
        )

        assert bridge.reacted[0][1] == "auto-id-42"
    finally:
        reset_current_channel_id(cid_token)
        set_channel_registry(None)


@pytest.mark.asyncio
async def test_send_message_without_actions_unchanged():
    """Plain text with no <actions> block is unaffected — no stripping,
    no extra react calls."""
    bridge = _CaptureBridge()
    set_channel_registry(_make_registry(bridge))
    cid_token = set_current_channel_id("cap-test")
    try:
        await send_message.ainvoke({"text": "just a normal reply"})

        assert bridge.sent[0][1] == "just a normal reply"
        assert bridge.reacted == []
    finally:
        reset_current_channel_id(cid_token)
        set_channel_registry(None)


@pytest.mark.asyncio
async def test_send_message_records_bridge_name_as_source(tmp_path):
    """send_message buffer append uses bridge.name as source so the
    message passes the recent_sources allowlist filter (chainlink #270).

    Previously source=None was hard-coded, which caused all outbound
    messages — including cross-channel sends from poller/heartbeat turns
    — to be excluded from ## Recent activity when the production
    allowlist (discord,slack,bluesky,web,stdin) was active.
    """
    from pathlib import Path
    from mimir.history import MessageBuffer, get_global_buffer, set_global_buffer

    buf = MessageBuffer(
        history_path=tmp_path / "chat_history.jsonl",
        global_max=50,
        per_channel_max=20,
    )
    prev_buf = get_global_buffer()
    set_global_buffer(buf)
    # Use a bridge whose name matches a known recent_sources entry.
    bridge = _CaptureBridge(name="discord", prefixes=("discord-",))
    set_channel_registry(_make_registry(bridge))
    cid_token = set_current_channel_id("discord-test-chan")
    try:
        result = await send_message.ainvoke({"text": "hello from poller turn"})
        assert "send_message ok" in result

        # Buffer should have one assistant message with source="discord".
        msgs = buf.recent_for_channel("discord-test-chan", limit=10)
        assert len(msgs) == 1
        assert msgs[0].source == "discord", (
            f"Expected source='discord', got {msgs[0].source!r}. "
            "Without this, messages are filtered by the production allowlist."
        )

        # Critically: it survives the production allowlist filter used in
        # recent_for_channel / assemble_recent_activity.
        prod_allowlist = frozenset({"discord", "slack", "bluesky", "web", "stdin"})
        msgs_filtered = buf.recent_for_channel(
            "discord-test-chan", limit=10, source_allowlist=prod_allowlist
        )
        assert len(msgs_filtered) == 1, (
            "Message was filtered out by prod allowlist — "
            "source=None not in allowlist."
        )
    finally:
        reset_current_channel_id(cid_token)
        set_channel_registry(None)
        set_global_buffer(prev_buf)  # type: ignore[arg-type]
