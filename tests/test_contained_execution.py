from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import mimir.contained_execution as contained
from mimir.contained_execution import (
    SensitiveMaterialScrubber,
    base_worker_environment,
    execute_contained,
    opencode_worker_environment,
)


class Capability:
    path = Path("/issued/checkout")


class Process:
    def __init__(self, stdout: bytes, stderr: bytes, *, immediate: bool = True) -> None:
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(stdout)
        self.stdout.feed_eof()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_data(stderr)
        self.stderr.feed_eof()
        self.release = asyncio.Event()
        if immediate:
            self.release.set()
        self.returncode: int | None = None

    async def wait(self) -> int:
        await self.release.wait()
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class Client:
    def __init__(
        self, stdout: bytes = b"", stderr: bytes = b"", *, immediate: bool = True
    ) -> None:
        self.process = Process(stdout, stderr, immediate=immediate)
        self.launched: list[dict[str, Any]] = []
        self.cancelled: list[str] = []

    async def launch(self, **kwargs: Any) -> Process:
        self.launched.append(kwargs)
        return self.process

    async def cancel(self, identifier: str) -> None:
        self.cancelled.append(identifier)
        self.process.returncode = -15
        self.process.release.set()


def install_client(monkeypatch: pytest.MonkeyPatch, client: Client) -> None:
    monkeypatch.setattr(contained, "WorkerClient", lambda _directory: client)


@pytest.mark.asyncio
async def test_execute_contained_returns_only_a_capped_collected_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = Client(b"abcdef", b"12345")
    install_client(monkeypatch, client)

    result = await execute_contained(
        ("tool", "arg"),
        Capability(),
        {"PATH": "/usr/bin"},
        identifier="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        timeout_s=1,
        stdout_limit=4,
        stderr_limit=3,
    )

    assert result.exit_code == 0
    assert result.stdout == b"abcd"
    assert result.stderr == b"123"
    assert result.output_overflow is True
    assert result.stdout_dropped_bytes == 2
    assert result.stderr_dropped_bytes == 2
    assert client.cancelled == ["aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"]
    assert client.launched[0]["local_checkout"] == Capability.path


@pytest.mark.asyncio
async def test_execute_contained_timeout_cancels_and_reaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = Client(b"partial", b"error", immediate=False)
    install_client(monkeypatch, client)

    result = await execute_contained(
        ("tool",),
        Capability(),
        {},
        identifier="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        timeout_s=0.01,
        stdout_limit=100,
        stderr_limit=100,
    )

    assert result.timed_out is True
    assert result.exit_code == -15
    assert result.stdout == b"partial"
    assert result.stderr == b"error"
    assert client.cancelled == ["aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"]


@pytest.mark.asyncio
async def test_execute_contained_cancellation_sends_cancel_before_reraising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = Client(immediate=False)
    install_client(monkeypatch, client)
    task = asyncio.create_task(
        execute_contained(
            ("tool",),
            Capability(),
            {},
            identifier="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            timeout_s=10,
            stdout_limit=100,
            stderr_limit=100,
        )
    )
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.cancelled == ["aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"]
    assert client.process.returncode == -15


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"argv": ()},
        {"argv": ("bad\x00arg",)},
        {"worker_env": {"HOME": "/controller"}},
        {"stdout_limit": 0},
        {"stderr_limit": -1},
        {"timeout_s": 0},
    ],
)
async def test_execute_contained_rejects_invalid_inputs_before_launch(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any]
) -> None:
    client = Client()
    install_client(monkeypatch, client)
    values: dict[str, Any] = {
        "argv": ("tool",),
        "directory": Capability(),
        "worker_env": {},
        "identifier": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "timeout_s": 1,
        "stdout_limit": 1,
        "stderr_limit": 1,
    }
    values.update(overrides)

    with pytest.raises(ValueError):
        await execute_contained(**values)
    assert client.launched == []


def test_worker_environment_builders_are_closed() -> None:
    identifier = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    base = base_worker_environment(identifier)

    assert set(base) == {
        "USER",
        "LOGNAME",
        "SHELL",
        "PATH",
        "LANG",
        "LC_ALL",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
    }
    assert "HOME" not in base
    assert "OPENCODE_CONFIG" not in base
    env = opencode_worker_environment(
        base,
        {"OPENCODE_PERMISSION": '{"edit":"allow"}', "MIMIR_SPAWN_DEPTH": "1"},
    )
    assert env["OPENCODE_CONFIG"].startswith(
        "/var/lib/mimir-worklink/homes/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/"
    )
    with pytest.raises(ValueError):
        opencode_worker_environment(base, {"OPENAI_API_KEY": "secret"})


def test_sensitive_material_scrubber_covers_bytes_and_path_forms(tmp_path: Path) -> None:
    home = tmp_path / 'controller "home"'
    checkout = tmp_path / "checkout"
    scrubber = SensitiveMaterialScrubber(home=home, checkout=checkout)
    scrubber.add_document(b"arbitrary credential bytes")
    scrubber.add_scalar("scalar-secret")

    payload = (
        f"{home} {home.as_uri()} scalar-secret ".encode()
        + b"arbitrary credential bytes"
    )
    scrubbed = scrubber.scrub_bytes(payload)
    assert str(home).encode() not in scrubbed
    assert home.as_uri().encode() not in scrubbed
    assert b"scalar-secret" not in scrubbed
    assert b"arbitrary credential bytes" not in scrubbed
    assert scrubbed.count(b"<redacted>") >= 3
    assert "_materials" not in repr(scrubber)
    assert scrubber.contains_sensitive(b"proposal scalar-secret") is True


_SECRET = b"sk-live-ABCDEFGHIJKLMNOPQRSTUV"
_TRUNCATION_OUTPUT = b"begin " + _SECRET + b" end-of-log"


def _secret_scrubber() -> SensitiveMaterialScrubber:
    scrubber = SensitiveMaterialScrubber(home="", checkout=None)
    scrubber.add_scalar(_SECRET)
    return scrubber


def _surviving_fragment(scrubber: SensitiveMaterialScrubber, emitted: bytes) -> bytes:
    """The longest non-empty piece of the secret left after scrubbing ``emitted``."""
    scrubbed = scrubber.scrub_bytes(emitted)
    for length in range(len(_SECRET), 0, -1):
        if _SECRET[:length] in scrubbed:
            return _SECRET[:length]
    return b""


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", range(1, len(_TRUNCATION_OUTPUT)))
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
async def test_truncation_never_emits_a_fragment_of_a_secret(
    monkeypatch: pytest.MonkeyPatch, limit: int, stream: str
) -> None:
    """No cut point may leave a piece of a secret that whole-value scrubbing misses.

    Every offset is exercised because the cut lands wherever the cap falls: a
    single hand-picked limit passes while the neighbouring ones leak.
    """
    on_stdout = stream == "stdout"
    client = Client(
        _TRUNCATION_OUTPUT if on_stdout else b"",
        b"" if on_stdout else _TRUNCATION_OUTPUT,
    )
    install_client(monkeypatch, client)
    scrubber = _secret_scrubber()

    result = await execute_contained(
        ("tool",),
        Capability(),
        {},
        identifier="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        timeout_s=1,
        stdout_limit=limit if on_stdout else 4096,
        stderr_limit=4096 if on_stdout else limit,
        scrubber=scrubber,
    )

    emitted = result.stdout if on_stdout else result.stderr
    dropped = result.stdout_dropped_bytes if on_stdout else result.stderr_dropped_bytes
    assert _surviving_fragment(scrubber, emitted) == b""
    assert len(emitted) <= limit
    # Trimming past the cap stays honest: the counter accounts for every byte
    # the caller does not receive, not just the ones the cap itself dropped.
    assert dropped == len(_TRUNCATION_OUTPUT) - len(emitted)


@pytest.mark.asyncio
async def test_truncation_keeps_the_whole_cap_when_no_secret_spans_the_cut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Output a secret was never in is capped exactly, not trimmed defensively.

    The cut is chosen from secrets actually spanning it rather than from tails
    that merely look like the start of one, so a run of a material's leading
    byte -- ``/`` is the first byte of every registered path -- costs nothing.
    """
    payload = b"/usr/lib/" + b"/" * 4000 + b"trailing"
    client = Client(payload)
    install_client(monkeypatch, client)
    scrubber = _secret_scrubber()
    scrubber.add_scalar("/mimir-home/state/liveness.json")

    result = await execute_contained(
        ("tool",),
        Capability(),
        {},
        identifier="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        timeout_s=1,
        stdout_limit=2000,
        stderr_limit=2000,
        scrubber=scrubber,
    )

    assert result.stdout == payload[:2000]
    assert result.stdout_dropped_bytes == len(payload) - 2000


def test_safe_truncation_length_moves_the_cut_off_a_spanning_secret() -> None:
    scrubber = _secret_scrubber()
    payload = _TRUNCATION_OUTPUT
    start = payload.index(_SECRET)

    assert scrubber.lookahead_bytes() == len(_SECRET) - 1
    # A cut anywhere inside the secret retreats to where it begins.
    for limit in range(start + 1, start + len(_SECRET)):
        assert scrubber.safe_truncation_length(payload, limit) == start
    # Cuts clear of it are left alone, including the two boundaries.
    for limit in (0, start, start + len(_SECRET), len(payload)):
        assert scrubber.safe_truncation_length(payload, limit) == limit
    # A limit past the buffer cannot invent bytes.
    assert scrubber.safe_truncation_length(payload, len(payload) + 50) == len(payload)


def test_safe_truncation_length_without_materials_is_the_limit() -> None:
    scrubber = SensitiveMaterialScrubber(home="", checkout=None)

    assert scrubber.lookahead_bytes() == 0
    assert scrubber.safe_truncation_length(b"plain output", 5) == 5
