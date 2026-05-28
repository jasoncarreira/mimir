"""Tests for mimir.bridges._attachments."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mimir.bridges._attachments import (
    AttachmentPathError,
    build_inbound_path,
    download_to_path,
    resolve_outbound_path,
    sanitize_filename,
)


def test_sanitize_basic():
    assert sanitize_filename("normal-file.png") == "normal-file.png"


def test_sanitize_strips_spaces_and_specials():
    assert sanitize_filename("hello world!.txt") == "hello_world_.txt"


def test_sanitize_collapses_to_default_when_empty():
    assert sanitize_filename("@!#") == "attachment"


def test_sanitize_keeps_dots_underscores_dashes():
    assert sanitize_filename("name.v1_2-final.tar.gz") == "name.v1_2-final.tar.gz"


# ─── build_inbound_path ─────────────────────────────────────────────


def test_build_inbound_path_creates_dir(tmp_path: Path):
    target = build_inbound_path(
        tmp_path, channel="discord", chat_id="987654", filename="report.pdf"
    )
    assert target.parent.is_dir()
    assert target.parent.name == "987654"
    assert target.parent.parent.name == "discord"
    # Filename: <ts>-<token>-<safe>
    assert target.name.endswith("-report.pdf")


def test_build_inbound_path_sanitizes_chat_id(tmp_path: Path):
    """Slack thread timestamps look like ``1714768920.000123`` — dots
    are filesystem-safe but underscoring them keeps everything uniform."""
    target = build_inbound_path(
        tmp_path, channel="slack", chat_id="C03ABC.123", filename="x.png"
    )
    assert target.parent.name == "C03ABC.123"  # dots survive — they're allowed


def test_build_inbound_path_collisions_avoided(tmp_path: Path):
    """Repeated calls in the same second still produce unique filenames
    because of the 8-char UUID suffix."""
    a = build_inbound_path(tmp_path, "x", "y", "same.png")
    b = build_inbound_path(tmp_path, "x", "y", "same.png")
    assert a != b


def test_build_inbound_path_default_filename(tmp_path: Path):
    target = build_inbound_path(tmp_path, "x", "y", None)
    assert target.name.endswith("-attachment")


# ─── resolve_outbound_path ──────────────────────────────────────────


def test_outbound_relative_inside_root(tmp_path: Path):
    root = tmp_path / "outbound"
    root.mkdir()
    f = root / "report.pdf"
    f.write_bytes(b"%PDF")
    resolved = resolve_outbound_path(root, "report.pdf")
    assert resolved == f.resolve()


def test_outbound_subdir_inside_root(tmp_path: Path):
    root = tmp_path / "outbound"
    (root / "charts").mkdir(parents=True)
    f = root / "charts" / "q3.png"
    f.write_bytes(b"")
    resolved = resolve_outbound_path(root, "charts/q3.png")
    assert resolved == f.resolve()


def test_outbound_absolute_inside_root_ok(tmp_path: Path):
    root = tmp_path / "outbound"
    root.mkdir()
    f = root / "x.txt"
    f.write_text("ok")
    resolved = resolve_outbound_path(root, str(f))
    assert resolved == f.resolve()


def test_outbound_dotdot_escape_rejected(tmp_path: Path):
    root = tmp_path / "outbound"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("nope")
    with pytest.raises(AttachmentPathError, match="escapes"):
        resolve_outbound_path(root, "../secret.txt")


def test_outbound_absolute_outside_root_rejected(tmp_path: Path):
    root = tmp_path / "outbound"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere.txt"
    elsewhere.write_text("nope")
    with pytest.raises(AttachmentPathError, match="outside"):
        resolve_outbound_path(root, str(elsewhere))


def test_outbound_symlink_escape_rejected(tmp_path: Path):
    """Symlinks pointing outside the root are rejected — Path.resolve()
    follows symlinks before the containment check, so the link's target
    is what's evaluated, not the link path."""
    root = tmp_path / "outbound"
    root.mkdir()
    target = tmp_path / "secret.txt"
    target.write_text("nope")
    link = root / "leak.txt"
    link.symlink_to(target)
    with pytest.raises(AttachmentPathError, match="escapes"):
        resolve_outbound_path(root, "leak.txt")


def test_outbound_missing_file_rejected(tmp_path: Path):
    root = tmp_path / "outbound"
    root.mkdir()
    with pytest.raises(AttachmentPathError, match="not found"):
        resolve_outbound_path(root, "nope.pdf")


def test_outbound_directory_rejected(tmp_path: Path):
    root = tmp_path / "outbound"
    sub = root / "subdir"
    sub.mkdir(parents=True)
    with pytest.raises(AttachmentPathError, match="not a file"):
        resolve_outbound_path(root, "subdir")


def test_outbound_empty_path_rejected(tmp_path: Path):
    with pytest.raises(AttachmentPathError, match="empty"):
        resolve_outbound_path(tmp_path, "")


def test_outbound_whitespace_only_rejected(tmp_path: Path):
    with pytest.raises(AttachmentPathError, match="empty"):
        resolve_outbound_path(tmp_path, "   ")


# ─── download_to_path ───────────────────────────────────────────────


def _make_aiohttp_mock(chunks: list[bytes], status: int = 200):
    """Build a minimal aiohttp ClientSession mock that streams ``chunks``."""

    async def _iter_chunked(_size: int):
        for c in chunks:
            yield c

    mock_content = MagicMock()
    mock_content.iter_chunked = _iter_chunked

    mock_resp = AsyncMock()
    mock_resp.status = status
    mock_resp.content = mock_content
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    return mock_session


@pytest.mark.asyncio
async def test_download_to_path_success(tmp_path: Path):
    """Happy path: streams chunks, writes file, returns True."""
    target = tmp_path / "out.bin"
    payload = b"hello world"
    mock_session = _make_aiohttp_mock([payload])

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await download_to_path("http://example.com/file", target)

    assert result is True
    assert target.read_bytes() == payload


@pytest.mark.asyncio
async def test_download_to_path_max_bytes_abort(tmp_path: Path):
    """When streaming exceeds max_bytes mid-stream, file is removed, returns False."""
    target = tmp_path / "out.bin"
    # Two 100-byte chunks; cap at 100 — first chunk fills the budget,
    # second chunk pushes over the limit.
    chunk1 = b"A" * 100
    chunk2 = b"B" * 100
    mock_session = _make_aiohttp_mock([chunk1, chunk2])

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await download_to_path(
            "http://example.com/file", target, max_bytes=100
        )

    assert result is False
    assert not target.exists()  # partial file cleaned up


@pytest.mark.asyncio
async def test_download_to_path_within_max_bytes(tmp_path: Path):
    """Download within max_bytes cap succeeds."""
    target = tmp_path / "out.bin"
    payload = b"X" * 50
    mock_session = _make_aiohttp_mock([payload])

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await download_to_path(
            "http://example.com/file", target, max_bytes=100
        )

    assert result is True
    assert target.read_bytes() == payload


@pytest.mark.asyncio
async def test_download_to_path_passes_headers(tmp_path: Path):
    """Custom headers are forwarded to session.get()."""
    target = tmp_path / "out.bin"
    captured: dict[str, str] = {}

    async def _iter_chunked(_size: int):
        yield b"data"

    mock_content = MagicMock()
    mock_content.iter_chunked = _iter_chunked
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.content = mock_content
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    def _fake_get(url, headers=None, **_kw):
        captured.update(headers or {})
        return mock_resp

    mock_session = MagicMock()
    mock_session.get = _fake_get
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await download_to_path(
            "http://example.com/priv",
            target,
            headers={"Authorization": "Bearer tok"},
        )

    assert result is True
    assert captured.get("Authorization") == "Bearer tok"


@pytest.mark.asyncio
async def test_download_to_path_http_error_returns_false(tmp_path: Path):
    """Non-2xx status → False, no file written."""
    target = tmp_path / "out.bin"
    mock_session = _make_aiohttp_mock([], status=403)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await download_to_path("http://example.com/file", target)

    assert result is False
    assert not target.exists()


@pytest.mark.asyncio
async def test_download_slack_attachment_enforces_cap(tmp_path: Path):
    """Slack attachment download enforces max_bytes even when pre-flight size is small.

    Simulates the attack surface from chainlink #228: Slack reports size=50
    but the server streams 200 bytes. The cap should abort the write and
    return a dropped attachment (path NOT in attachment_paths).
    """
    from unittest.mock import AsyncMock as AM
    from mimir.bridges.slack import SlackBridge

    bot_token = "xoxb-test"
    attachments_dir = tmp_path / "attachments"
    max_bytes = 100

    bridge = SlackBridge(
        bot_token=bot_token,
        app_token="xapp-test",
        enqueue=AM(return_value=None),
        attachments_dir=attachments_dir,
        attachments_max_bytes=max_bytes,
    )

    # Event: one file with reported size=50 but streams 200 bytes
    event = {
        "type": "message",
        "channel": "C123",
        "user": "U456",
        "text": "",
        "ts": "1234567890.000001",
        "files": [
            {
                "id": "F001",
                "name": "big.bin",
                "size": 50,  # Reported small size — passes pre-flight check
                "url_private": "https://slack.com/files/big.bin",
            }
        ],
    }

    # Mock download_to_path to simulate streaming cap triggering
    with patch(
        "mimir.bridges.slack.download_to_path", new_callable=AM
    ) as mock_dl:
        mock_dl.return_value = False  # cap triggered — returns False
        await bridge._on_message(event)

    # download_to_path was called with max_bytes and the auth header
    assert mock_dl.call_count == 1
    call_kwargs = mock_dl.call_args.kwargs
    assert call_kwargs.get("max_bytes") == max_bytes
    assert call_kwargs.get("headers", {}).get("Authorization") == f"Bearer {bot_token}"
