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


def test_missing_leaf_does_not_hide_unsafe_ancestor_or_create_it(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.path.parent.parent.mkdir(mode=0o755)
    with pytest.raises(ProfileError, match="unsafe-profile-store"):
        s.set(Profile("default", Path("/x")))
    assert not s.path.parent.exists()


def test_symlinked_ancestor_is_rejected_without_creating_through_it(tmp_path: Path) -> None:
    s = store(tmp_path)
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    s.path.parent.parent.symlink_to(target, target_is_directory=True)
    with pytest.raises(ProfileError, match="unsafe-profile-store"):
        s.set(Profile("default", Path("/x")))
    assert not (target / "acp").exists()


def test_config_root_is_private_before_descendants_are_created(tmp_path: Path) -> None:
    s = store(tmp_path)
    os.chmod(tmp_path, 0o755)
    try:
        with pytest.raises(ProfileError, match="unsafe-profile-store"):
            s.set(Profile("default", Path("/x")))
        assert not (tmp_path / "mimir").exists()
    finally:
        os.chmod(tmp_path, 0o700)


def test_parent_traversal_component_is_rejected_without_mutation(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    s = ProfileStore(target / ".." / "escaped" / "mimir" / "acp" / "profiles.json")

    with pytest.raises(ProfileError, match="unsafe-profile-store"):
        s.set(Profile("default", Path("/x")))

    assert not (tmp_path / "escaped").exists()


def test_symlink_before_config_root_is_rejected_without_target_mutation(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    s = ProfileStore(link / "config" / "mimir" / "acp" / "profiles.json")

    with pytest.raises(ProfileError, match="unsafe-profile-store"):
        s.set(Profile("default", Path("/x")))

    assert list(target.iterdir()) == []


def test_write_keeps_validated_ancestor_descriptor_during_swap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = store(tmp_path)
    s.set(Profile("old", Path("/old")))
    mimir = s.path.parent.parent
    detached = tmp_path / "detached"
    attacker = tmp_path / "attacker"
    (attacker / "acp").mkdir(parents=True, mode=0o700)
    os.chmod(attacker, 0o700)
    os.chmod(attacker / "acp", 0o700)
    real_open = os.open
    acp_opens = 0

    def swapping_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal acp_opens
        if path == "acp" and kwargs.get("dir_fd") is not None:
            acp_opens += 1
            # set() traverses once to read and again to write.  Swap only after
            # the write traversal has already opened and validated ``mimir``.
            if acp_opens == 2:
                mimir.rename(detached)
                mimir.symlink_to(attacker, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr("mimir.acp.profiles.os.open", swapping_open)
    s.set(Profile("new", Path("/new")))

    assert acp_opens == 2
    assert not (attacker / "acp" / "profiles.json").exists()
    saved = json.loads((detached / "acp" / "profiles.json").read_text())
    assert set(saved["profiles"]) == {"old", "new"}

def test_list_is_sorted_names_only_canonical_json(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.set(Profile("z", Path("/z")))
    s.set(Profile("A", Path("/a")))
    assert [profile.name for profile in s.list()] == ["A", "z"]
    assert s.path.read_bytes() == b'{"profiles":{"A":{"home":"/a","remote":null,"timeoutSeconds":60},"z":{"home":"/z","remote":null,"timeoutSeconds":60}},"version":1}\n'
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
        if path == s.path.name and kwargs.get("dir_fd") is not None:
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


def test_timeout_field_is_backward_compatible_and_strict(tmp_path: Path) -> None:
    s = store(tmp_path)
    prepare(s, '{"profiles":{"legacy":{"home":"/legacy","remote":null}},"version":1}\n')
    assert s.get("legacy") == Profile("legacy", Path("/legacy"), timeout_seconds=60)

    s.set_timeout("legacy", 600)
    saved = json.loads(s.path.read_text())
    assert saved == {
        "profiles": {
            "legacy": {
                "home": "/legacy",
                "remote": None,
                "timeoutSeconds": 600,
            }
        },
        "version": 1,
    }
    assert s.get("legacy") == Profile("legacy", Path("/legacy"), timeout_seconds=600)

    invalid_values: tuple[object, ...] = (None, True, False, 0, 601, 1.0, "60")
    for value in invalid_values:
        payload = json.dumps({
            "profiles": {
                "p": {"home": "/p", "remote": None, "timeoutSeconds": value}
            },
            "version": 1,
        })
        s.path.write_text(payload)
        os.chmod(s.path, 0o600)
        before = s.path.read_bytes()
        with pytest.raises(ProfileError, match="invalid-profile-store"):
            s.list()
        assert s.path.read_bytes() == before

    s.path.write_text(
        '{"profiles":{"p":{"home":"/p","remote":null,"timeoutSeconds":60,"extra":1}},"version":1}'
    )
    os.chmod(s.path, 0o600)
    unknown = s.path.read_bytes()
    with pytest.raises(ProfileError, match="invalid-profile-store"):
        s.list()
    assert s.path.read_bytes() == unknown

    oversized = (
        '{"profiles":{"p":{"home":"/p","remote":null,"timeoutSeconds":'
        + "9" * 5000
        + '}},"version":1}'
    )
    s.path.write_text(oversized)
    os.chmod(s.path, 0o600)
    before = s.path.read_bytes()
    with pytest.raises(ProfileError, match="invalid-profile-store"):
        s.list()
    assert s.path.read_bytes() == before

    s.path.write_text('{"profiles":{},"version":1}\n')
    with pytest.raises(ProfileError, match="profile-not-found"):
        s.set_timeout("missing", 60)
    with pytest.raises(ProfileError, match="invalid-profile"):
        Profile("p", Path("/p"), timeout_seconds=0)
