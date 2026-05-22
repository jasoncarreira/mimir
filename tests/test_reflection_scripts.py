"""v0.4 §4: bundled reflection script tests.

The most_retrieved.py script is invoked from the reflection skill's
SKILL.md via Bash. It needs to (a) parse argv flags correctly and
(b) call SagaClient.most_retrieved_atoms with the right kwargs."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from typing import Any

import pytest

from mimir.reflection import most_retrieved as script


class _FakeClient:
    """Records the most_retrieved_atoms call args and returns a stub
    payload. Matches the SagaClient interface the script depends on."""

    def __init__(self, payload: list[dict[str, Any]] | None = None) -> None:
        self.payload = payload or [
            {"id": "atom-1", "content": "x", "retrieval_count": 5}
        ]
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    async def most_retrieved_atoms(
        self,
        *,
        days: int,
        count: int,
        channel_id: str | None,
        contributed_only: bool,
        trend: str | None = None,
    ) -> list[dict[str, Any]]:
        self.calls.append(
            {
                "days": days,
                "count": count,
                "channel_id": channel_id,
                "contributed_only": contributed_only,
                "trend": trend,
            }
        )
        return self.payload

    async def close(self) -> None:
        self.closed = True


def _patch_script(monkeypatch, fake_client: _FakeClient, argv: list[str]) -> None:
    """Replace the script's SagaClient constructor with one that returns
    fake_client, and the script's argv. Config.from_env happens for real
    but only reads env vars (which monkeypatch sets to harmless defaults)."""
    monkeypatch.setattr(script, "make_saga_client", lambda **kw: fake_client)
    monkeypatch.setattr("sys.argv", ["most_retrieved", *argv])
    monkeypatch.setenv("MIMIR_HOME", "/tmp/mimir-test")
    monkeypatch.setenv("SAGA_ENDPOINT", "http://example.invalid")


@pytest.mark.asyncio
async def test_default_args_pass_expected_kwargs(monkeypatch: pytest.MonkeyPatch):
    fake = _FakeClient()
    _patch_script(monkeypatch, fake, [])
    out = io.StringIO()
    with redirect_stdout(out):
        rc = await script._amain()
    assert rc == 0
    assert fake.calls == [
        {"days": 7, "count": 10, "channel_id": None, "contributed_only": False,
         "trend": None}
    ]
    assert fake.closed


@pytest.mark.asyncio
async def test_all_flags_threaded_through(monkeypatch: pytest.MonkeyPatch):
    fake = _FakeClient()
    _patch_script(
        monkeypatch,
        fake,
        ["--days", "14", "--count", "20", "--channel", "slack-eng",
         "--contributed-only"],
    )
    out = io.StringIO()
    with redirect_stdout(out):
        rc = await script._amain()
    assert rc == 0
    assert fake.calls == [
        {"days": 14, "count": 20, "channel_id": "slack-eng",
         "contributed_only": True, "trend": None}
    ]


@pytest.mark.asyncio
async def test_output_is_json_round_trippable(monkeypatch: pytest.MonkeyPatch):
    """stdout body must parse back to the client's return value so the
    skill can pipe it into Read or jq."""
    payload = [
        {"id": "atom-1", "content": "abc", "retrieval_count": 7},
        {"id": "atom-2", "content": "def", "retrieval_count": 4},
    ]
    fake = _FakeClient(payload=payload)
    _patch_script(monkeypatch, fake, [])
    out = io.StringIO()
    with redirect_stdout(out):
        await script._amain()
    parsed = json.loads(out.getvalue())
    assert parsed == payload


@pytest.mark.asyncio
async def test_client_is_always_closed_even_on_failure(monkeypatch: pytest.MonkeyPatch):
    class _Boom(_FakeClient):
        async def most_retrieved_atoms(self, **kwargs: Any) -> list[dict[str, Any]]:
            raise RuntimeError("simulated SAGA blow-up")

    fake = _Boom()
    _patch_script(monkeypatch, fake, [])
    with pytest.raises(RuntimeError, match="simulated"):
        await script._amain()
    assert fake.closed


def test_cli_subcommand_dispatches_to_script(monkeypatch: pytest.MonkeyPatch):
    """`mimir reflection most-retrieved` is the invocation the reflection
    skill uses. Smoke-test that the CLI parser routes to script.run with
    the expected args."""
    from mimir import cli

    fake = _FakeClient()
    monkeypatch.setattr(script, "make_saga_client", lambda **kw: fake)
    monkeypatch.setenv("SAGA_ENDPOINT", "http://example.invalid")

    # SystemExit is the normal flow when the CLI completes (sys.exit(0)).
    out = io.StringIO()
    with redirect_stdout(out), pytest.raises(SystemExit) as exc_info:
        cli.main(["reflection", "most-retrieved", "--days", "3",
                  "--count", "5", "--contributed-only"])
    assert exc_info.value.code == 0
    assert fake.calls == [
        {"days": 3, "count": 5, "channel_id": None, "contributed_only": True,
         "trend": None}
    ]
