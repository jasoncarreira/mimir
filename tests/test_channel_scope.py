"""Operator scope is enforced on inbound bridge messages before intake."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from mimir.bridges.channel_scope import ChannelScope, admit
from mimir.config import (
    Config, _SCOPE_ENV_KEYS, _load_home_dotenv, _protected_channel_scope_file,
    load_channel_scopes,
)


def decision(scope: ChannelScope, **overrides):
    args = dict(
        channel_id="discord-2", parent_channel_id=None, is_dm=False,
        mentioned=False, author_is_bot=False, author_id="discord-7",
    )
    args.update(overrides)
    return admit(scope, **args)


def test_default_admits_any_human_guild_channel():
    assert decision(ChannelScope(), channel_id="discord-987654") == (True, "admitted")
    assert decision(ChannelScope(), author_is_bot=True) == (False, "bot_author")


def test_ignored_has_priority_over_allow_all_mention_and_free_response():
    scope = ChannelScope(ignored=frozenset({"discord-2"}),
                         free_response=frozenset({"discord-2"}), require_mention=True)
    assert decision(scope, mentioned=True) == (False, "ignored_channel")
    assert decision(scope, mentioned=False) == (False, "ignored_channel")


def test_parent_inherits_allow_ignore_and_free_response():
    scope = ChannelScope(allowed=frozenset({"discord-1"}),
                         free_response=frozenset({"discord-1"}), require_mention=True)
    assert decision(scope, parent_channel_id="discord-1") == (True, "admitted")
    assert decision(scope) == (False, "channel_not_allowed")
    assert decision(replace(scope, ignored=frozenset({"discord-1"})),
                    parent_channel_id="discord-1", mentioned=True) == (False, "ignored_channel")


def test_dm_bypasses_channel_and_mention_but_not_bot_gate():
    scope = ChannelScope(allowed=frozenset(), ignored=frozenset({"dm-discord-2"}),
                         require_mention=True)
    assert decision(scope, channel_id="dm-discord-2", is_dm=True) == (True, "admitted")
    assert decision(scope, channel_id="dm-discord-2", is_dm=True,
                    author_is_bot=True) == (False, "bot_author")


@pytest.mark.parametrize("mode,mentioned,expected", [
    ("none", True, False), ("mentions", False, False),
    ("mentions", True, True), ("all", False, True),
])
def test_bot_modes(mode, mentioned, expected):
    assert decision(ChannelScope(allow_bots=mode), author_is_bot=True,
                    mentioned=mentioned)[0] is expected


def test_allowed_bot_id_bypasses_mention_but_not_channel_filters():
    scope = ChannelScope(allowed_bot_ids=frozenset({"discord-7"}), require_mention=True)
    assert decision(scope, author_is_bot=True) == (True, "admitted")
    assert decision(replace(scope, allowed=frozenset()),
                    author_is_bot=True) == (False, "channel_not_allowed")


def test_free_response_exempts_mention_only_in_matched_channels():
    scope = ChannelScope(require_mention=True, free_response=frozenset({"discord-2"}))
    assert decision(scope) == (True, "admitted")
    assert decision(scope, channel_id="discord-3") == (False, "mention_required")
    assert decision(scope, channel_id="discord-3", mentioned=True) == (True, "admitted")


def test_env_normalizes_bare_and_prefixed_ids(monkeypatch, tmp_path):
    monkeypatch.setenv("MIMIR_DISCORD_ALLOWED_CHANNELS", "1,discord-2")
    monkeypatch.setenv("MIMIR_SLACK_ALLOWED_CHANNELS", "*")
    monkeypatch.setenv("MIMIR_DISCORD_ALLOWED_BOT_IDS", "7,discord-8")
    scopes = load_channel_scopes(tmp_path)
    assert scopes["discord"].allowed == frozenset({"discord-1", "discord-2"})
    assert scopes["discord"].allowed_bot_ids == frozenset({"discord-7", "discord-8"})
    assert scopes["slack"].allowed is None
    monkeypatch.setenv("MIMIR_DISCORD_ALLOWED_CHANNELS", "")
    assert load_channel_scopes(tmp_path)["discord"].allowed == frozenset()
    monkeypatch.setenv("MIMIR_DISCORD_ALLOW_BOTS", "everybody")
    with pytest.raises(ValueError, match="allow_bots"):
        load_channel_scopes(tmp_path)


def test_home_dotenv_is_never_scope_source(monkeypatch, tmp_path, caplog):
    (tmp_path / ".env").write_text("MIMIR_DISCORD_ALLOWED_CHANNELS=1\n")
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    Config.from_env()
    assert load_channel_scopes(tmp_path)["discord"].allowed is None
    assert "MIMIR_DISCORD_ALLOWED_CHANNELS" in caplog.text
    assert "MIMIR_DISCORD_ALLOWED_CHANNELS" not in os.environ
    monkeypatch.setenv("MIMIR_DISCORD_ALLOWED_CHANNELS", "2")
    assert load_channel_scopes(tmp_path)["discord"].allowed == frozenset({"discord-2"})


@pytest.mark.parametrize("key", [
    "MIMIR_CLAUDE_OAUTH_CREDENTIALS", "MIMIR_SYSTEM_PROMPT_OVERRIDE",
])
def test_home_dotenv_preserves_explicit_empty_values(key, monkeypatch, tmp_path):
    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text(f"{key}=\n")
    _load_home_dotenv(tmp_path)
    assert os.environ[key] == ""


@pytest.mark.parametrize("key", sorted(_SCOPE_ENV_KEYS))
@pytest.mark.parametrize("process_value", [None, "operator-value", ""])
def test_home_dotenv_never_adds_or_overrides_scope_keys(
    key, process_value, monkeypatch, tmp_path, caplog,
):
    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.delenv(key, raising=False)
    if process_value is not None:
        monkeypatch.setenv(key, process_value)
    (tmp_path / ".env").write_text(f"{key}=home-value\n")
    _load_home_dotenv(tmp_path)
    assert os.environ.get(key) == process_value
    assert key in caplog.text
    assert "home-value" not in caplog.text
    assert "operator-value" not in caplog.text


def test_home_dotenv_interpolation_prefers_process_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.setenv("DOTENV_SOURCE", "process")
    for key in ("DOTENV_RESULT", "DOTENV_BARE"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text(
        "DOTENV_SOURCE=home\nDOTENV_RESULT=${DOTENV_SOURCE}\nDOTENV_BARE\n"
    )
    loaded = _load_home_dotenv(tmp_path)
    assert os.environ["DOTENV_SOURCE"] == "process"
    assert os.environ["DOTENV_RESULT"] == "process"
    assert "DOTENV_BARE" not in os.environ
    assert loaded == ["DOTENV_RESULT"]


def test_scope_env_keys_are_isolated_from_host():
    source = Path(__file__).with_name("conftest.py").read_text()
    for key in {
        "MIMIR_CHANNEL_SCOPE_FILE",
        *(f"MIMIR_{platform}_{key}" for platform in ("DISCORD", "SLACK")
          for key in ("ALLOWED_CHANNELS", "IGNORED_CHANNELS", "REQUIRE_MENTION",
                      "FREE_RESPONSE_CHANNELS", "ALLOW_BOTS", "ALLOWED_BOT_IDS")),
    }:
        assert f'"{key}"' in source


@pytest.fixture
def protected_file(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    policy = tmp_path / "policy.yaml"
    policy.write_text('discord:\n  allowed_channels: ["1"]\nslack:\n  allowed_channels: ["C1"]\n')
    # Drive the same os.access guard independently of root/non-root test uid.
    monkeypatch.setattr("mimir.config.os.access", lambda path, mode: False)
    monkeypatch.setenv("MIMIR_CHANNEL_SCOPE_FILE", str(policy))
    return policy, home


@pytest.mark.parametrize("key,attribute", [
    ("allowed_channels", "allowed"), ("ignored_channels", "ignored"),
    ("free_response_channels", "free_response"), ("allowed_bot_ids", "allowed_bot_ids"),
])
def test_file_normalizes_unquoted_discord_integers(key, attribute, protected_file):
    policy, home = protected_file
    policy.write_text(f'discord:\n  {key}: [1234567890123456789, "discord-2"]\n')
    scopes = load_channel_scopes(home)
    assert getattr(scopes["discord"], attribute) == frozenset({
        "discord-1234567890123456789", "discord-2",
    })
    assert scopes["slack"] == ChannelScope()


@pytest.mark.parametrize("key", [
    "allowed_channels", "ignored_channels", "free_response_channels", "allowed_bot_ids",
])
@pytest.mark.parametrize("value", ["true", "false", "1.5"])
def test_file_rejects_discord_booleans_and_floats(key, value, protected_file):
    policy, home = protected_file
    policy.write_text(f"discord:\n  {key}: [{value}]\n")
    with pytest.raises(ValueError, match="ids must be nonempty strings"):
        load_channel_scopes(home)


def test_file_rejects_integer_slack_ids(protected_file):
    policy, home = protected_file
    policy.write_text("slack:\n  allowed_channels: [123]\n")
    with pytest.raises(ValueError, match="ids must be nonempty strings"):
        load_channel_scopes(home)


def test_protected_file_loads_and_enforces_both_platforms(protected_file):
    _, home = protected_file
    scopes = load_channel_scopes(home)
    assert decision(scopes["discord"], channel_id="discord-1") == (True, "admitted")
    assert decision(scopes["discord"]) == (False, "channel_not_allowed")
    assert decision(scopes["slack"], channel_id="slack-C2") == (False, "channel_not_allowed")


@pytest.mark.asyncio
async def test_protected_file_wired_through_both_live_bridge_handlers(
    protected_file, monkeypatch,
):
    from mimir import server
    policy, home = protected_file
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv("DISCORD_TOKEN", "test")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "test")
    app = server.build_app(Config.from_env())
    bridges = {b.name: b for b in app["channels"].bridges()}
    assert {"discord", "slack"} <= bridges.keys()
    assert bridges["discord"].channel_scope == load_channel_scopes(home)["discord"]
    assert bridges["slack"].channel_scope == load_channel_scopes(home)["slack"]
    discord = pytest.importorskip("discord")
    discord_bridge = bridges["discord"]
    discord_bridge.admit = Mock(return_value=True)
    discord_bridge.enqueue = AsyncMock(return_value=True)
    discord_bridge._client = SimpleNamespace(user=SimpleNamespace(id=42))
    discord_bridge.send_typing_indicator = AsyncMock()
    slack_bridge = bridges["slack"]
    slack_bridge.admit = Mock(return_value=True)
    slack_bridge.enqueue = AsyncMock(return_value=True)
    slack_bridge._app = SimpleNamespace(client=SimpleNamespace(users_info=AsyncMock(return_value={})))
    slack_bridge._bot_user_id = "UBOT"
    for cid, expected in ((1, True), (2, False)):
        message = SimpleNamespace(
            id=cid, channel=SimpleNamespace(id=cid, type=discord.ChannelType.text, name="eng"),
            author=SimpleNamespace(id=7, bot=False, display_name="human"),
            content="hello", mentions=[], reference=None, attachments=[],
        )
        await discord_bridge._on_message(message)
        assert discord_bridge.admit.call_count == discord_bridge.enqueue.await_count == int(expected)
        event = {"channel": f"C{cid}", "channel_type": "channel", "user": "U7",
                 "text": "hello", "ts": str(cid)}
        await slack_bridge._on_message(event)
        assert slack_bridge.admit.call_count == slack_bridge.enqueue.await_count == int(expected)
        discord_bridge.admit.reset_mock()
        discord_bridge.enqueue.reset_mock()
        slack_bridge.admit.reset_mock()
        slack_bridge.enqueue.reset_mock()


def test_scope_has_no_model_reachable_runtime_reader():
    # The only loader call is in server construction; bridge handlers consult
    # their immutable scope instance rather than environment, files or tools.
    root = Path(__file__).resolve().parents[1] / "mimir"
    readers = []
    for path in root.rglob("*.py"):
        if "load_channel_scopes(" in path.read_text():
            readers.append(path.relative_to(root).as_posix())
    assert sorted(readers) == ["config.py", "server.py"]


@pytest.mark.parametrize("failure", ["file", "directory", "home", "symlink",
                                   "yaml", "key", "platform", "conflict", "null_list"])
def test_bad_file_refuses_both_bridges_and_logs_content_free(
    failure, protected_file, monkeypatch,
):
    from mimir import server
    policy, home = protected_file
    marker = "DISTINCTIVE_SECRET_IN_POLICY"
    if failure == "file":
        monkeypatch.setattr("mimir.config.os.access", lambda path, mode: Path(path) == policy)
    elif failure == "directory":
        monkeypatch.setattr("mimir.config.os.access", lambda path, mode: Path(path) == policy.parent)
    elif failure == "home":
        inside = home / "scope.yaml"
        inside.write_text(policy.read_text())
        monkeypatch.setenv("MIMIR_CHANNEL_SCOPE_FILE", str(inside))
    elif failure == "symlink":
        link = policy.parent / "link.yaml"
        link.symlink_to(policy)
        monkeypatch.setenv("MIMIR_CHANNEL_SCOPE_FILE", str(link))
        monkeypatch.setattr("mimir.config.os.access", lambda path, mode: Path(path) == policy)
    elif failure == "yaml":
        policy.write_text(f"discord: [{marker}\n")
    elif failure == "key":
        policy.write_text(f"discord:\n  unexpected: {marker}\n")
    elif failure == "platform":
        policy.write_text(f"elsewhere: {marker}\n")
    elif failure == "conflict":
        monkeypatch.setenv("MIMIR_SLACK_ALLOW_BOTS", "all")
    elif failure == "null_list":
        policy.write_text("discord:\n  allowed_channels: null\n")
    monkeypatch.setenv("DISCORD_TOKEN", "test")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "test")
    monkeypatch.setenv("MIMIR_HOME", str(home))
    events = []
    monkeypatch.setattr("mimir.event_logger.log_event_sync",
                        lambda kind, **fields: events.append((kind, fields)))
    app = server.build_app(Config.from_env())
    names = {b.name for b in app["channels"].bridges()}
    assert "discord" not in names and "slack" not in names
    rejected = [fields for kind, fields in events if kind == "channel_scope_config_rejected"]
    assert len(rejected) == 1
    assert marker not in str(rejected)


def test_protection_checks_target_and_every_ancestor(protected_file, monkeypatch):
    policy, home = protected_file
    visited = []
    def check(path, mode):
        visited.append(Path(path))
        return False
    monkeypatch.setattr("mimir.config.os.access", check)
    assert _protected_channel_scope_file(policy, home) == policy.resolve()
    assert visited == [policy.resolve(), *policy.resolve().parents]


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario,expected", [
    ("allowed", True), ("other", False), ("thread_allowed", True),
    ("thread_other", False), ("ignored", False), ("bot", False),
    ("approved_bot", True), ("unmentioned", False), ("mentioned", True),
    ("reply", True), ("free", True),
])
async def test_discord_scope_before_intake_and_download(scenario, expected, monkeypatch, tmp_path):
    discord = pytest.importorskip("discord")
    from mimir.bridges import discord as module
    scope = ChannelScope(allowed=frozenset({"discord-1", "discord-3"}),
                         ignored=frozenset({"discord-3"}),
                         require_mention=scenario in {"unmentioned", "mentioned", "reply", "free"},
                         free_response=frozenset({"discord-1"}) if scenario == "free" else frozenset(),
                         allowed_bot_ids=frozenset({"discord-7"}) if scenario == "approved_bot" else frozenset())
    cid = 2 if scenario in {"other", "thread_other"} else 3 if scenario == "ignored" else 1
    if scenario in {"thread_allowed", "thread_other"}:
        parent = 1 if scenario == "thread_allowed" else 2
        cid = 99
        typ = discord.ChannelType.public_thread
    else:
        parent = None
        typ = discord.ChannelType.text
    channel = SimpleNamespace(id=cid, parent_id=parent, type=typ, name="channel")
    author = SimpleNamespace(id=7 if scenario in {"bot", "approved_bot"} else 8,
                             bot=scenario in {"bot", "approved_bot"}, display_name="DISTINCTIVE_NAME")
    message = SimpleNamespace(
        id=101, author=author, channel=channel, content="DISTINCTIVE_MESSAGE",
        mentions=[SimpleNamespace(id=42)] if scenario == "mentioned" else [],
        reference=SimpleNamespace(message_id=55, resolved=SimpleNamespace(author=SimpleNamespace(id=42)))
        if scenario == "reply" else None,
        attachments=[SimpleNamespace(url="https://cdn.discordapp.com/a", filename="a", size=1)],
    )
    enqueue = AsyncMock(return_value=True)
    intake = Mock(return_value=True)
    download = AsyncMock(return_value=True)
    logs = AsyncMock()
    monkeypatch.setattr(module, "_safe_log_event", logs)
    monkeypatch.setattr("mimir.bridges._attachments.download_to_path", download)
    bridge = module.DiscordBridge(token="test", enqueue=enqueue, admit=intake,
                                  channel_scope=scope, attachments_dir=tmp_path)
    bridge._client = SimpleNamespace(user=SimpleNamespace(id=42))
    bridge.send_typing_indicator = AsyncMock()
    await bridge._on_message(message)
    assert intake.call_count == enqueue.await_count == download.await_count == int(expected)
    assert logs.await_count == (not expected)
    if not expected:
        kind, kwargs = logs.await_args.args[0], logs.await_args.kwargs
        assert kind == "channel_scope_dropped"
        assert set(kwargs) == {"platform", "channel_id", "parent_channel_id", "reason", "author_kind"}
        assert "DISTINCTIVE_MESSAGE" not in str(kwargs)
        assert "DISTINCTIVE_NAME" not in str(kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario,expected", [
    ("allowed", True), ("other", False), ("thread_allowed", True),
    ("thread_other", False), ("ignored", False), ("bot", False),
    ("approved_bot", True), ("unmentioned", False), ("mentioned", True),
    ("free", True),
])
async def test_slack_scope_before_intake_lookup_and_download(scenario, expected, monkeypatch, tmp_path):
    from mimir.bridges import slack as module
    scope = ChannelScope(allowed=frozenset({"slack-C1", "slack-C3"}),
                         ignored=frozenset({"slack-C3"}),
                         require_mention=scenario in {"unmentioned", "mentioned", "free"},
                         free_response=frozenset({"slack-C1"}) if scenario == "free" else frozenset(),
                         allowed_bot_ids=frozenset({"slack-U7"}) if scenario == "approved_bot" else frozenset())
    cid = "C2" if scenario in {"other", "thread_other"} else "C3" if scenario == "ignored" else "C1"
    event = dict(channel=cid, channel_type="channel", user="U7" if scenario in {"bot", "approved_bot"} else "U8",
                 bot_id="B7" if scenario in {"bot", "approved_bot"} else None,
                 text="<@UBOT> DISTINCTIVE_MESSAGE" if scenario == "mentioned" else "DISTINCTIVE_MESSAGE",
                 ts="123.45", files=[{"url_private": "https://files.slack.com/a", "name": "a", "size": 1}])
    if scenario.startswith("thread_"):
        event["thread_ts"] = "123.00"
    enqueue = AsyncMock(return_value=True)
    intake = Mock(return_value=True)
    download = AsyncMock(return_value=True)
    users_info = AsyncMock(return_value={"user": {"real_name": "DISTINCTIVE_NAME"}})
    logs = AsyncMock()
    monkeypatch.setattr(module, "_safe_log_event", logs)
    monkeypatch.setattr(module, "download_to_path", download)
    bridge = module.SlackBridge(bot_token="x", app_token="x", enqueue=enqueue,
                                admit=intake, channel_scope=scope, attachments_dir=tmp_path)
    bridge._bot_user_id = "UBOT"
    bridge._app = SimpleNamespace(client=SimpleNamespace(users_info=users_info))
    await bridge._on_message(event)
    assert intake.call_count == enqueue.await_count == download.await_count == users_info.await_count == int(expected)
    assert logs.await_count == (not expected)
    if not expected:
        assert logs.await_args.args == ("channel_scope_dropped",)
        assert set(logs.await_args.kwargs) == {"platform", "channel_id", "parent_channel_id", "reason", "author_kind"}
        assert "DISTINCTIVE_MESSAGE" not in str(logs.await_args)
        assert "DISTINCTIVE_NAME" not in str(logs.await_args)
