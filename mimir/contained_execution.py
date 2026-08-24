from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .output_capture import OutputSink, open_output_pair

if TYPE_CHECKING:
    from .worklink.worker_client import CheckoutCapability, WorkerProjection

WorkerClient: Any = None

__all__ = (
    "CollectedExecutionResult",
    "SensitiveMaterialScrubber",
    "base_worker_environment",
    "execute_contained",
    "opencode_worker_environment",
)

_RUNTIME_HOME_ROOT = Path("/var/lib/mimir-worklink/homes")
_REDACTION = b"<redacted>"


@dataclass(frozen=True)
class CollectedExecutionResult:
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool
    output_overflow: bool
    stdout_dropped_bytes: int
    stderr_dropped_bytes: int


def base_worker_environment(identifier: str) -> dict[str, str]:
    home = _RUNTIME_HOME_ROOT / identifier
    return {
        "USER": "worklink",
        "LOGNAME": "worklink",
        "SHELL": "/bin/sh",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "XDG_CONFIG_HOME": f"{home}/.config",
        "XDG_DATA_HOME": f"{home}/.local/share",
        "XDG_CACHE_HOME": f"{home}/.cache",
    }


def opencode_worker_environment(
    base: Mapping[str, str], invocation: Mapping[str, str]
) -> dict[str, str]:
    env = dict(base)
    if "HOME" in env:
        raise ValueError("worker HOME is assigned by the executor")
    config_home = env.get("XDG_CONFIG_HOME")
    if not config_home:
        raise ValueError("worker XDG_CONFIG_HOME is required")
    allowed = {
        "OPENCODE_PERMISSION",
        "OPENCODE_MODEL",
        "OPENCODE_PROVIDER",
        "OPENCODE_AGENT",
        "MIMIR_SPAWN_DEPTH",
    }
    unknown = set(invocation) - allowed
    if unknown:
        raise ValueError(f"OpenCode worker environment {sorted(unknown)[0]} is denied")
    for name, value in invocation.items():
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ValueError(f"OpenCode worker environment {name} is invalid")
        env[name] = value
    env["OPENCODE_CONFIG"] = f"{config_home}/opencode/opencode.json"
    return env


class SensitiveMaterialScrubber:
    __slots__ = ("_materials",)

    def __init__(
        self,
        *,
        home: Path | str | None = None,
        checkout: Path | str | None = None,
        artifact_root: Path | str | None = None,
        source_paths: Sequence[Path | str] = (),
    ) -> None:
        self._materials: set[bytes] = set()
        controller_home = home if home is not None else os.environ.get("HOME")
        known_paths: list[Path | str] = list(source_paths)
        if controller_home is not None and str(controller_home):
            home_path = Path(controller_home).expanduser()
            config_root = home_path / ".config"
            data_root = home_path / ".local/share"
            known_paths.extend((
                config_root / "opencode/opencode.json",
                config_root / "opencode/opencode.jsonc",
                data_root / "opencode/auth.json",
            ))
        explicit_config = os.environ.get("OPENCODE_CONFIG")
        if explicit_config:
            known_paths.append(explicit_config)
        for path in (controller_home, checkout, artifact_root, *known_paths):
            if path is not None and str(path):
                self.add_path(path)

    def __repr__(self) -> str:
        return "SensitiveMaterialScrubber()"

    def add_path(self, path: Path | str) -> None:
        lexical = os.path.expanduser(os.fspath(path))
        forms = {lexical, str(Path(lexical).resolve(strict=False))}
        for value in tuple(forms):
            candidate = Path(value)
            if candidate.is_absolute():
                forms.add(candidate.as_uri())
        for value in tuple(forms):
            encoded = json.dumps(value, ensure_ascii=False)
            forms.add(encoded)
            forms.add(encoded[1:-1])
        for value in forms:
            self.add_scalar(value)

    def add_document(self, document: bytes | bytearray | memoryview | str) -> None:
        self._add(document)

    def add_scalar(self, value: bytes | bytearray | memoryview | str) -> None:
        self._add(value)

    def contains_sensitive(self, value: bytes | bytearray | memoryview | str) -> bool:
        payload = self._bytes(value)
        return any(material in payload for material in self._materials)

    def scrub_bytes(self, value: bytes | bytearray | memoryview | str) -> bytes:
        payload = self._bytes(value)
        for material in sorted(self._materials, key=len, reverse=True):
            payload = payload.replace(material, _REDACTION)
        return payload

    def scrub_text(
        self, value: bytes | bytearray | memoryview | str, *, errors: str = "replace"
    ) -> str:
        return self.scrub_bytes(value).decode("utf-8", errors=errors)

    def lookahead_bytes(self) -> int:
        """Bytes to read past a cut so a material spanning it is present in full."""
        if not self._materials:
            return 0
        return max(len(material) for material in self._materials) - 1

    def safe_truncation_length(
        self, value: bytes | bytearray | memoryview, limit: int
    ) -> int:
        """Largest cut at or below ``limit`` that no registered material spans.

        ``scrub_bytes`` replaces whole values, so a buffer cut through the middle
        of a material keeps a fragment that matches nothing and survives
        scrubbing verbatim. Truncation is the only step that produces such a
        fragment -- a value split across reads is reassembled before scrubbing --
        so the cut is where the boundary has to be enforced.

        ``value`` must extend ``lookahead_bytes()`` past ``limit`` wherever the
        source has that much left, so an occurrence spanning ``limit`` is present
        whole and can be located rather than inferred from its prefix. Inferring
        would move the cut for any tail that merely looks like the start of a
        material, which discards output no secret was ever in.
        """
        payload = self._bytes(value)
        cut = max(0, min(limit, len(payload)))
        while True:
            for material in self._materials:
                length = len(material)
                # The bounds admit only occurrences that start before ``cut`` and
                # end after it, so every hit genuinely spans the cut and sits
                # strictly earlier -- which is also why this terminates.
                found = payload.find(
                    material, max(0, cut - length + 1), cut + length - 1
                )
                if found != -1:
                    cut = found
                    break
            else:
                return cut

    def _add(self, value: bytes | bytearray | memoryview | str) -> None:
        payload = self._bytes(value)
        if payload:
            self._materials.add(payload)

    @staticmethod
    def _bytes(value: bytes | bytearray | memoryview | str) -> bytes:
        if isinstance(value, str):
            return value.encode("utf-8")
        return bytes(value)


def _worker_classes() -> tuple[Any, Any]:
    global WorkerClient
    from .worklink.worker_client import (
        WorkerClient as Client,
        WorkerProjection as Projection,
    )

    if WorkerClient is None:
        WorkerClient = Client
    return WorkerClient, Projection


async def execute_contained(
    argv: Sequence[str],
    directory: CheckoutCapability,
    worker_env: Mapping[str, str],
    projections: Sequence[WorkerProjection] = (),
    *,
    identifier: str,
    timeout_s: float,
    stdout_limit: int,
    stderr_limit: int,
    scrubber: Any = None,
    stdout_sink: OutputSink | None = None,
    stderr_sink: OutputSink | None = None,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
) -> CollectedExecutionResult:
    if stdout_limit <= 0 or stderr_limit <= 0:
        raise ValueError("worker output limits must be positive")
    if timeout_s <= 0:
        raise ValueError("worker timeout must be positive")
    if isinstance(argv, (str, bytes)) or not argv or any(
        not isinstance(arg, str) or not arg or "\x00" in arg for arg in argv
    ):
        raise ValueError("worker command must contain non-empty NUL-free strings")
    if "HOME" in worker_env:
        raise ValueError("worker HOME is assigned by the executor")
    client_class, projection_class = _worker_classes()
    try:
        checked_projections = tuple(
            projection_class(path=item.path, document=item.document) for item in projections
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("worker projection is invalid") from exc
    if len(checked_projections) > 2 or len(
        {item.path for item in checked_projections}
    ) != len(checked_projections):
        raise ValueError("worker projections must use at most two unique destinations")

    opened_here = stdout_sink is None and stderr_sink is None
    if (stdout_sink is None) != (stderr_sink is None):
        raise ValueError("contained worker output sinks must be supplied together")
    if stdout_sink is None or stderr_sink is None:
        stdout_sink, stderr_sink = open_output_pair(
            stdout_path, stdout_limit, stderr_path, stderr_limit
        )
    client = (
        getattr(directory, "_contained_worker_client", None) or client_class(directory)
    )
    try:
        process = await client.launch(
            local_checkout=directory.path,
            argv=tuple(argv),
            env=dict(worker_env),
            projections=checked_projections,
            identifier=identifier,
            timeout_s=timeout_s,
            stdout_sink=stdout_sink,
            stderr_sink=stderr_sink,
        )
    except BaseException:
        if opened_here:
            stdout_sink.close()
            stderr_sink.close()
        raise
    started = getattr(directory, "_contained_started", None)
    if started is not None:
        started(process)
    output_overflow = False
    cancel_task: asyncio.Task[None] | None = None

    async def monitor_output() -> None:
        nonlocal output_overflow, cancel_task
        while True:
            overflow = await asyncio.to_thread(
                lambda: stdout_sink.overflowed() or stderr_sink.overflowed()
            )
            if overflow and not output_overflow:
                output_overflow = True
                cancel_task = asyncio.create_task(client.cancel(identifier))
            if overflow:
                lookahead = scrubber.lookahead_bytes() if scrubber is not None else 0
                stdout_sink.truncate_to_limit(lookahead)
                stderr_sink.truncate_to_limit(lookahead)
            await asyncio.sleep(0.01)

    async def collect() -> int | None:
        return await process.wait()

    monitor_task = asyncio.create_task(monitor_output())
    collect_task = asyncio.create_task(collect())
    timed_out = False
    try:
        try:
            exit_code = await asyncio.wait_for(asyncio.shield(collect_task), timeout_s)
        except TimeoutError:
            timed_out = True
            try:
                await client.cancel(identifier)
            except Exception:
                pass
            finally:
                exit_code = await collect_task
    except asyncio.CancelledError:
        await asyncio.shield(client.cancel(identifier))
        try:
            await asyncio.shield(collect_task)
        finally:
            raise
    monitor_task.cancel()
    await asyncio.gather(monitor_task, return_exceptions=True)
    if cancel_task is not None:
        await cancel_task
    stdout, stdout_dropped = stdout_sink.read_bounded(scrubber=scrubber)
    stderr, stderr_dropped = stderr_sink.read_bounded(scrubber=scrubber)
    observed_overflow = stdout_sink.did_overflow or stderr_sink.did_overflow
    if observed_overflow and not output_overflow:
        output_overflow = True
        await client.cancel(identifier)
    stdout_sink.truncate_to_limit()
    stderr_sink.truncate_to_limit()
    result = CollectedExecutionResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out or getattr(process, "timed_out", False),
        output_overflow=output_overflow,
        stdout_dropped_bytes=stdout_dropped,
        stderr_dropped_bytes=stderr_dropped,
    )
    if opened_here:
        stdout_sink.close()
        stderr_sink.close()
    return result
