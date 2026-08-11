from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from mimir.acp.daemon import AcpDaemon, AcpDaemonError, acp_enabled_from_env


class _Channels:
    def register(self, bridge: object) -> None:
        pass


def _short_home() -> Path:
    return Path(tempfile.mkdtemp(prefix="mimir-acp-", dir="/tmp"))


def _bundle(home: Path) -> SimpleNamespace:
    resolver = SimpleNamespace(_yaml_path=home / "state" / "identities.yaml")
    core = SimpleNamespace(identity_resolver=resolver)
    adapters = SimpleNamespace(channels=_Channels())
    return SimpleNamespace(
        config=SimpleNamespace(home=home, acp_journal_ttl_days=7),
        core=core,
        adapters=adapters,
    )


def test_enabled_false_values_skip_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mimir.acp.daemon.peer_credentials_supported", lambda: False)
    for value in ("0", "FALSE", " No ", "off", "N"):
        monkeypatch.setenv("MIMIR_ACP_ENABLED", value)
        assert acp_enabled_from_env() is False


def test_explicit_enable_fails_without_peer_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mimir.acp.daemon.peer_credentials_supported", lambda: False)
    monkeypatch.setenv("MIMIR_ACP_ENABLED", "true")
    with pytest.raises(AcpDaemonError, match="unsupported"):
        acp_enabled_from_env()


@pytest.mark.asyncio
async def test_daemon_creates_owner_only_socket_and_removes_it(tmp_path: Path) -> None:
    home = _short_home()
    daemon = AcpDaemon(_bundle(home))
    await daemon.start()
    assert stat.S_IMODE(daemon.directory.stat().st_mode) == 0o700
    socket_stat = daemon.socket_path.lstat()
    assert stat.S_ISSOCK(socket_stat.st_mode)
    assert stat.S_IMODE(socket_stat.st_mode) == 0o600
    assert socket_stat.st_uid == os.getuid()
    await daemon.stop()
    assert not daemon.socket_path.exists()
    shutil.rmtree(home)


@pytest.mark.asyncio
async def test_daemon_rejects_symlink_directory(tmp_path: Path) -> None:
    home = _short_home()
    target = home / "target"
    target.mkdir()
    (home / ".mimir").mkdir()
    (home / ".mimir" / "acp").symlink_to(target, target_is_directory=True)
    daemon = AcpDaemon(_bundle(home))
    with pytest.raises(AcpDaemonError, match="symlink"):
        await daemon.start()


@pytest.mark.asyncio
async def test_live_socket_is_not_unlinked(tmp_path: Path) -> None:
    home = _short_home()
    first = AcpDaemon(_bundle(home))
    await first.start()
    identity = first.socket_path.lstat().st_ino
    second = AcpDaemon(_bundle(home))
    with pytest.raises(AcpDaemonError, match="already listening"):
        await second.start()
    assert first.socket_path.lstat().st_ino == identity
    await first.stop()
    shutil.rmtree(home)
