from __future__ import annotations

import ipaddress
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_USER = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,31}\Z")
_DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")

class ProfileError(ValueError):
    def __init__(self, code: str = "invalid-profile") -> None:
        super().__init__(code)
        self.code = code

@dataclass(frozen=True, slots=True)
class RemoteProfile:
    host: str
    user: str
    port: int
    identity_file: Path
    known_hosts_file: Path

    def __post_init__(self) -> None:
        if not valid_host(self.host) or not isinstance(self.user, str) or not _USER.fullmatch(self.user):
            raise ProfileError()
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not 1 <= self.port <= 65535:
            raise ProfileError()
        for value in (self.identity_file, self.known_hosts_file):
            path = Path(value)
            if not path.is_absolute() or len(str(path)) > 4096 or _bad_text(str(path)):
                raise ProfileError()
        object.__setattr__(self, "identity_file", Path(self.identity_file))
        object.__setattr__(self, "known_hosts_file", Path(self.known_hosts_file))

@dataclass(frozen=True, slots=True)
class Profile:
    name: str
    home: Path
    remote: RemoteProfile | None = None

    def __post_init__(self) -> None:
        validate_profile_name(self.name)
        home = Path(self.home)
        text = str(home)
        if not PurePosixPath(text).is_absolute() or len(text) > 4096 or _bad_text(text):
            raise ProfileError()
        object.__setattr__(self, "home", home)


def _bad_text(value: str) -> bool:
    return "\x00" in value or "\n" in value or "\r" in value


def validate_profile_name(name: str) -> str:
    if not isinstance(name, str) or _NAME.fullmatch(name) is None:
        raise ProfileError()
    return name


def valid_host(host: str) -> bool:
    if not isinstance(host, str) or not host or len(host) > 253 or _bad_text(host) or host.startswith("[") or host.endswith("]"):
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        labels = host[:-1].split(".") if host.endswith(".") else host.split(".")
        return bool(labels) and all(_DNS_LABEL.fullmatch(label) for label in labels)


def selected_profile(explicit: str | None = None, environ: Mapping[str, str] | None = None) -> str:
    if explicit is not None:
        return validate_profile_name(explicit)
    value = (os.environ if environ is None else environ).get("MIMIR_" + "ACP_PROFILE", "").strip()
    return validate_profile_name(value) if value else "default"


def _pairs(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise ProfileError("invalid-profile-store")
        result[key] = value
    return result


class ProfileStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or self.default_path()

    @staticmethod
    def default_path(environ: Mapping[str, str] | None = None) -> Path:
        values = os.environ if environ is None else environ
        root = Path(values.get("XDG_CONFIG_HOME", "") or Path.home() / ".config")
        if not root.is_absolute():
            raise ProfileError("unsafe-profile-store")
        return root / "mimir" / "acp" / "profiles.json"

    def list(self) -> list[Profile]:
        return [self._decode(name, value) for name, value in sorted(self._read().items())]

    def get(self, name: str) -> Profile | None:
        validate_profile_name(name)
        data = self._read().get(name)
        return None if data is None else self._decode(name, data)

    def set(self, profile: Profile) -> None:
        values = self._read()
        values[profile.name] = self._encode(profile)
        self._write(values)

    put = set

    def delete(self, name: str) -> None:
        validate_profile_name(name)
        values = self._read()
        if name not in values:
            raise ProfileError("profile-not-found")
        del values[name]
        self._write(values)

    def remove(self, name: str) -> bool:
        try:
            self.delete(name)
            return True
        except ProfileError as exc:
            if exc.code == "profile-not-found":
                return False
            raise

    def _read(self) -> dict[str, object]:
        self._validate_parents_if_present()
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, flags)
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise ProfileError("unsafe-profile-store") from exc
        try:
            value = os.fstat(fd)
            if not stat.S_ISREG(value.st_mode) or value.st_uid != os.getuid() or value.st_mode & 0o077:
                raise ProfileError("unsafe-profile-store")
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                fd = -1
                document = json.load(stream, object_pairs_hook=_pairs)
        except ProfileError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProfileError("invalid-profile-store") from exc
        finally:
            if fd >= 0:
                os.close(fd)
        if not isinstance(document, dict):
            raise ProfileError("invalid-profile-store")
        version = document.get("version")
        if set(document) != {"version", "profiles"}:
            if "version" not in document or isinstance(version, bool) or not isinstance(version, int):
                raise ProfileError("unsupported-profile-version")
            raise ProfileError("invalid-profile-store")
        if isinstance(version, bool) or not isinstance(version, int) or version != 1:
            raise ProfileError("unsupported-profile-version")
        profiles = document["profiles"]
        if not isinstance(profiles, dict):
            raise ProfileError("invalid-profile-store")
        for name, data in profiles.items():
            try:
                self._decode(name, data)
            except ProfileError as exc:
                if exc.code == "invalid-profile":
                    raise ProfileError("invalid-profile-store") from exc
                raise
        return dict(profiles)

    def _validate_parents(self) -> None:
        uid = os.getuid()
        for directory in (self.path.parent.parent, self.path.parent):
            try:
                value = directory.lstat()
            except OSError as exc:
                raise ProfileError("unsafe-profile-store") from exc
            if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode) or value.st_uid != uid or value.st_mode & 0o077:
                raise ProfileError("unsafe-profile-store")

    def _validate_parents_if_present(self) -> None:
        uid = os.getuid()
        for directory in (self.path.parent.parent, self.path.parent):
            try:
                value = directory.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ProfileError("unsafe-profile-store") from exc
            if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode) or value.st_uid != uid or value.st_mode & 0o077:
                raise ProfileError("unsafe-profile-store")

    def _write(self, profiles: Mapping[str, object]) -> None:
        self._ensure_parents()
        try:
            existing = self.path.lstat()
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode) or existing.st_uid != os.getuid() or existing.st_mode & 0o077:
                raise ProfileError("unsafe-profile-store")
        payload = json.dumps({"version": 1, "profiles": dict(profiles)}, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        fd, name = tempfile.mkstemp(prefix=".profiles.", dir=self.path.parent)
        temporary = Path(name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                fd = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try: os.fsync(directory_fd)
            finally: os.close(directory_fd)
        finally:
            if fd >= 0: os.close(fd)
            try: temporary.unlink()
            except FileNotFoundError: pass

    def _ensure_parents(self) -> None:
        self._validate_parents_if_present()
        missing: list[Path] = []
        current = self.path.parent
        while not current.exists():
            missing.append(current); current = current.parent
        for directory in reversed(missing):
            directory.mkdir(mode=0o700)
            os.chmod(directory, 0o700)
        self._validate_parents()

    @staticmethod
    def _encode(profile: Profile) -> dict[str, object]:
        remote = None if profile.remote is None else {
            "host": profile.remote.host, "user": profile.remote.user, "port": profile.remote.port,
            "identityFile": str(profile.remote.identity_file), "knownHostsFile": str(profile.remote.known_hosts_file),
        }
        return {"home": str(profile.home), "remote": remote}

    @staticmethod
    def _decode(name: object, data: object) -> Profile:
        if not isinstance(name, str) or not isinstance(data, dict) or set(data) != {"home", "remote"}:
            raise ProfileError()
        home, remote_data = data["home"], data["remote"]
        if not isinstance(home, str): raise ProfileError()
        remote = None
        if remote_data is not None:
            required = {"host", "user", "port", "identityFile", "knownHostsFile"}
            if not isinstance(remote_data, dict) or set(remote_data) != required: raise ProfileError()
            values = [remote_data[k] for k in ("host", "user", "port", "identityFile", "knownHostsFile")]
            if not all(isinstance(v, str) for v in (values[0], values[1], values[3], values[4])): raise ProfileError()
            remote = RemoteProfile(values[0], values[1], values[2], Path(values[3]), Path(values[4]))
        return Profile(name, Path(home), remote)


def profile_json(profile: Profile) -> dict[str, object]:
    return {"version": 1, "name": profile.name, **ProfileStore._encode(profile)}
