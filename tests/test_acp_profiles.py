from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mimir.acp.profiles import Profile, ProfileError, ProfileStore, RemoteProfile, selected_profile


def store(tmp_path: Path) -> ProfileStore:
    return ProfileStore(tmp_path / "mimir" / "acp" / "profiles.json")


def prepare(s: ProfileStore, payload: str, mode: int = 0o600) -> bytes:
    s.path.parent.mkdir(parents=True, mode=0o700)
    os.chmod(s.path.parent.parent, 0o700)
    os.chmod(s.path.parent, 0o700)
    s.path.write_text(payload, encoding="utf-8")
    os.chmod(s.path, mode)
    return s.path.read_bytes()


def test_list_absent_store_is_empty_without_creating_it(tmp_path: Path) -> None:
    s = store(tmp_path)
    assert s.list() == []
    assert not s.path.exists()


def test_list_is_sorted_names_only_canonical_json(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.set(Profile("z", Path("/z")))
    s.set(Profile("A", Path("/a")))
    assert [profile.name for profile in s.list()] == ["A", "z"]
    assert s.path.read_bytes() == b'{"profiles":{"A":{"home":"/a","remote":null},"z":{"home":"/z","remote":null}},"version":1}\n'
    assert s.path.stat().st_mode & 0o777 == 0o600
    assert s.path.parent.stat().st_mode & 0o777 == 0o700


def test_show_local_and_remote_exact_json(tmp_path: Path) -> None:
    s = store(tmp_path)
    local = Profile("local", Path("/srv/mimir"))
    remote = Profile("remote", Path("/srv/remote"), RemoteProfile("example.com", "agent", 2222, Path("/id"), Path("/known")))
    s.set(local)
    s.set(remote)
    assert s.get("local") == local
    assert s.get("remote") == remote


def test_show_missing_store_or_profile(tmp_path: Path) -> None:
    s = store(tmp_path)
    assert s.get("missing") is None
    assert not s.path.exists()
    s.set(Profile("other", Path("/other")))
    assert s.get("missing") is None


@pytest.mark.parametrize("mode", [0o644, 0o620])
def test_unsafe_store_is_rejected_without_rewrite(tmp_path: Path, mode: int) -> None:
    s = store(tmp_path)
    before = prepare(s, '{"profiles":{},"version":1}\n', mode)
    with pytest.raises(ProfileError, match="unsafe-profile-store"):
        s.set(Profile("default", Path("/x")))
    assert s.path.read_bytes() == before


def test_symlink_store_is_rejected_without_rewrite(tmp_path: Path) -> None:
    s = store(tmp_path)
    target = tmp_path / "target"
    target.write_text('{"profiles":{},"version":1}\n')
    s.path.parent.mkdir(parents=True, mode=0o700)
    os.chmod(s.path.parent.parent, 0o700)
    os.chmod(s.path.parent, 0o700)
    s.path.symlink_to(target)
    with pytest.raises(ProfileError, match="unsafe-profile-store"):
        s.list()
    assert target.read_text() == '{"profiles":{},"version":1}\n'


def test_read_opens_no_follow_and_rejects_path_swap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = store(tmp_path)
    prepare(s, '{"profiles":{},"version":1}\n')
    target = tmp_path / "target"
    target.write_text('{"profiles":{"stolen":{"home":"/x","remote":null}},"version":1}\n')
    real_open = os.open
    observed: list[int] = []

    def swapping_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if Path(path) == s.path:
            observed.append(flags)
            s.path.unlink()
            s.path.symlink_to(target)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr("mimir.acp.profiles.os.open", swapping_open)
    with pytest.raises(ProfileError, match="unsafe-profile-store"):
        s.list()
    assert observed and observed[0] & getattr(os, "O_NOFOLLOW", 0)
    assert "stolen" in target.read_text()


@pytest.mark.parametrize("payload", [
    '{"version":1,"version":1,"profiles":{}}',
    '{"version":1,"profiles":{"p":{"home":"/x","home":"/y","remote":null}}}',
    '{"version":1,"profiles":{},"extra":1}',
    '{"version":1,"profiles":{"p":{"home":"/x","remote":null,"extra":1}}}',
    '{"version":1,"profiles":{"p":{"home":"/x","remote":{"host":"x","user":"u","port":22,"identityFile":"/i","knownHostsFile":"/k","extra":1}}}}',
    "not-json",
])
def test_malformed_duplicate_and_unknown_fields_are_rejected(tmp_path: Path, payload: str) -> None:
    s = store(tmp_path)
    before = prepare(s, payload)
    with pytest.raises(ProfileError, match="invalid-profile-store"):
        s.list()
    assert s.path.read_bytes() == before


@pytest.mark.parametrize("payload", [
    '{"profiles":{}}',
    '{"version":true,"profiles":{}}',
    '{"version":"1","profiles":{}}',
    '{"version":2,"profiles":{}}',
])
def test_unsupported_or_invalid_version_is_never_rewritten(tmp_path: Path, payload: str) -> None:
    s = store(tmp_path)
    before = prepare(s, payload)
    with pytest.raises(ProfileError, match="unsupported-profile-version"):
        s.set(Profile("default", Path("/x")))
    assert s.path.read_bytes() == before


def test_set_replaces_local_and_remote_atomically(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.set(Profile("p", Path("/remote"), RemoteProfile("example.com", "agent", 22, Path("/id"), Path("/known"))))
    s.set(Profile("p", Path("/local")))
    assert s.get("p") == Profile("p", Path("/local"))
    assert json.loads(s.path.read_text())["profiles"]["p"]["remote"] is None


@pytest.mark.parametrize("factory", [
    lambda: Profile("bad name", Path("/x")),
    lambda: Profile("p", Path("relative")),
    lambda: RemoteProfile("bad host!", "user", 22, Path("/id"), Path("/known")),
    lambda: RemoteProfile("example.com", "bad user", 22, Path("/id"), Path("/known")),
    lambda: RemoteProfile("example.com", "user", 0, Path("/id"), Path("/known")),
])
def test_set_validation_and_partial_ssh_group_do_not_mutate(factory: object) -> None:
    with pytest.raises(ProfileError, match="invalid-profile"):
        factory()


def test_delete_profile_only_and_persist_empty_store(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.set(Profile("default", Path("/x")))
    s.delete("default")
    assert s.path.read_bytes() == b'{"profiles":{},"version":1}\n'


def test_delete_missing_profile_is_error(tmp_path: Path) -> None:
    s = store(tmp_path)
    with pytest.raises(ProfileError, match="profile-not-found"):
        s.delete("default")
    assert not s.path.exists()


def test_selection_precedence() -> None:
    assert selected_profile("explicit", {"MIMIR_ACP_PROFILE": "env"}) == "explicit"
    assert selected_profile(None, {"MIMIR_ACP_PROFILE": " env "}) == "env"
    assert selected_profile(None, {"MIMIR_ACP_PROFILE": "  "}) == "default"
