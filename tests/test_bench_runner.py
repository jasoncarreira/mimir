"""Regression tests for ``benchmarks/longmemeval_via_mimir/runner.py``.

Companion to ``test_bench_via_mimir.py`` (which covers the deterministic
route/score pieces). This file holds contract assertions about runner
setup that protect against re-introduction of live-bridge leaks into
bench-mode mimirs.

See chainlink #119 (regression guard for PR #142, commit 64a821f).
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest


def test_suppress_production_bridges_in_env_blocks_live_bridge_registration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression guard for chainlink #119 (PR #142, commit 64a821f).

    Failure mode this test pins:
        ``mimir/server.py:build_app`` registers ``DiscordBridge`` and
        ``SlackBridge`` if ``DISCORD_TOKEN`` / ``SLACK_BOT_TOKEN`` /
        ``SLACK_APP_TOKEN`` are present in the env. When mimir launches the
        longmemeval bench as a subagent, those tokens are inherited from
        the parent shell — so each bench-mimir-instance authenticates as
        the production bot account, receives every live inbound, runs a
        full agent turn on it, and replies. PR #142's fix is the
        ``_suppress_production_bridges_in_env`` call in the runner's
        ``_amain`` prelude. The failure mode is invisible from inside the
        bench (events route to live Discord, not bench logs) and easy for
        a future PR to relax back to ``setdefault`` "to allow operator
        override."

    Test shape:
        1. Set the three tokens to non-empty placeholder values
           (simulates an operator's .env or a parent shell that has them
           exported).
        2. Invoke the bench-runner's env-clear helper directly — this is
           the same call site ``_amain`` makes before ``Config.from_env()``.
        3. Sanity-check the env state is now empty for all three vars.
        4. Build the app via the same path the bench runner uses
           (``Config.from_env() -> build_app``).
        5. Assert neither ``DiscordBridge`` nor ``SlackBridge`` appears in
           ``app["channels"].bridges()``.

    The chainlink description offered the alternative of patching
    ``DiscordBridge.__init__`` / ``SlackBridge.__init__`` to raise. That's
    unnecessary here because ``app["channels"].bridges()`` exposes the
    registered bridges cleanly. The list-membership assertion gives a
    clearer failure message than a constructor-raise would.
    """
    from benchmarks.longmemeval_via_mimir.runner import (
        _BENCH_PRODUCTION_BRIDGE_ENV_VARS,
        _suppress_production_bridges_in_env,
    )
    from mimir import server as mimir_server
    from mimir.config import Config

    # The helper's own promise — useful to pin so a future PR that adds a
    # fourth bridge env var (or removes one) updates the test in lockstep.
    assert _BENCH_PRODUCTION_BRIDGE_ENV_VARS == (
        "DISCORD_TOKEN",
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
    )

    # Step 1: simulate operator env with all three tokens set (non-empty).
    monkeypatch.setenv("DISCORD_TOKEN", "fake_discord_token")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake-bot-token")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-fake-app-token")
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    # Step 2: invoke the bench-runner's env-clear helper. This is the
    # load-bearing logic from PR #142 — a future PR that removes this
    # call (or relaxes the unconditional ``= ""`` to ``setdefault``) will
    # fail this test.
    _suppress_production_bridges_in_env()

    # Step 3: sanity-check the helper actually cleared the vars, so we
    # know the subsequent build_app sees empty tokens (not the
    # monkeypatched non-empty values).
    assert os.environ.get("DISCORD_TOKEN") == ""
    assert os.environ.get("SLACK_BOT_TOKEN") == ""
    assert os.environ.get("SLACK_APP_TOKEN") == ""

    # Step 4: build the app via the same path the bench runner uses.
    cfg = Config.from_env()
    cfg = replace(cfg, home=tmp_path)
    app = mimir_server.build_app(cfg)

    # Step 5: assert no live bridges are registered.
    bridge_type_names = {type(b).__name__ for b in app["channels"].bridges()}

    assert "DiscordBridge" not in bridge_type_names, (
        f"DiscordBridge registered despite cleared DISCORD_TOKEN — "
        f"this means the runner's env-clear was bypassed or build_app "
        f"changed how it reads the token. Bench-mimirs will reply to "
        f"live Discord. See PR #142 / chainlink #119. "
        f"Bridges seen: {bridge_type_names}"
    )
    assert "SlackBridge" not in bridge_type_names, (
        f"SlackBridge registered despite cleared SLACK_BOT_TOKEN / "
        f"SLACK_APP_TOKEN — same failure mode as Discord. "
        f"See PR #142 / chainlink #119. "
        f"Bridges seen: {bridge_type_names}"
    )


def test_amain_calls_suppress_production_bridges_before_config_from_env() -> None:
    """Pin that ``_amain`` actually invokes the env-clear helper.

    The behavioural test above calls ``_suppress_production_bridges_in_env``
    directly — so a regression that removed the *call* from ``_amain``
    (leaving the helper definition intact) would slip past it. This test
    closes that gap by source-inspecting ``_amain`` and asserting (a) it
    invokes the helper, and (b) the invocation precedes
    ``Config.from_env()`` so the cleared tokens propagate.
    """
    import inspect

    from benchmarks.longmemeval_via_mimir.runner import _amain

    source = inspect.getsource(_amain)

    helper_pos = source.find("_suppress_production_bridges_in_env()")
    # Anchor on the assignment pattern so the comment reference
    # ("``Config.from_env()``") above the helper call doesn't match first.
    config_pos = source.find("cfg = Config.from_env()")

    assert helper_pos != -1, (
        "_amain no longer calls _suppress_production_bridges_in_env(). "
        "Without that call, bench-mimirs inherit DISCORD_TOKEN / "
        "SLACK_BOT_TOKEN / SLACK_APP_TOKEN from the parent shell and "
        "register live bridges. See PR #142 / chainlink #119."
    )
    assert config_pos != -1, (
        "_amain no longer calls Config.from_env() — has the build path "
        "moved? Re-check that bridge tokens are still suppressed before "
        "Config construction. See PR #142 / chainlink #119."
    )
    assert helper_pos < config_pos, (
        "_suppress_production_bridges_in_env() must run BEFORE "
        "Config.from_env() — otherwise Config reads the inherited "
        "non-empty tokens and build_app registers live bridges. "
        f"helper at offset {helper_pos}, Config.from_env at {config_pos}. "
        "See PR #142 / chainlink #119."
    )
