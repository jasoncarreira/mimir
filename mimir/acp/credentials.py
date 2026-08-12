from __future__ import annotations

import getpass
import os
import re
from typing import Any, Callable

SERVICE = "mimir.acp"
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")

class CredentialError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code

class CredentialMutationUncertain(CredentialError):
    def __init__(self) -> None:
        super().__init__("credential-mutation-uncertain")


def _validate(name: str) -> None:
    if not isinstance(name, str) or _NAME.fullmatch(name) is None:
        raise CredentialError("invalid-profile")


def _is_native(backend: Any) -> bool:
    cls = type(backend)
    return (cls.__module__, cls.__name__) in {
        ("keyring.backends.macOS", "Keyring"),
        ("keyring.backends.SecretService", "Keyring"),
        ("keyring.backends.Windows", "WinVaultKeyring"),
    }


def _production_backend() -> Any:
    try:
        import keyring
        backend = keyring.get_keyring()
        if (type(backend).__module__, type(backend).__name__) == ("keyring.backends.chainer", "ChainerBackend"):
            candidates = [item for item in backend.backends if _is_native(item)]
            backend = max(candidates, key=lambda item: item.priority) if candidates else None
        if backend is None or not _is_native(backend) or float(backend.priority) <= 0:
            raise RuntimeError
    except BaseException as exc:
        raise CredentialError("secure-store-unavailable") from exc
    return backend


class NativeCredentialStore:
    def __init__(self, *, _backend: Any | None = None) -> None:
        self._backend = _backend

    def _native(self) -> Any:
        if self._backend is None:
            self._backend = _production_backend()
        return self._backend

    def require_available(self) -> None:
        self._native()

    def get(self, profile: str) -> str | None:
        _validate(profile)
        try:
            value = self._native().get_password(SERVICE, profile)
        except CredentialError:
            raise
        except BaseException as exc:
            raise CredentialError("credential-read-failed") from exc
        if value is not None and not isinstance(value, str):
            raise CredentialError("credential-read-failed")
        return value

    def status(self, profile: str) -> bool:
        return self.get(profile) is not None

    def set(self, profile: str, secret: str) -> None:
        _validate(profile)
        backend = self._native()
        try:
            backend.set_password(SERVICE, profile, secret)
        except BaseException as exc:
            raise CredentialMutationUncertain() from exc

    def delete(self, profile: str) -> None:
        _validate(profile)
        backend = self._native()
        try:
            present = backend.get_password(SERVICE, profile)
        except BaseException as exc:
            raise CredentialError("credential-read-failed") from exc
        if present is None:
            return
        try:
            backend.delete_password(SERVICE, profile)
        except BaseException as exc:
            raise CredentialMutationUncertain() from exc

    remove = delete

CredentialStore = NativeCredentialStore


def read_secret_from_tty(prompt: Callable[..., str] = getpass.getpass) -> str:
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOCTTY", 0)
    try:
        fd = os.open("/dev/tty", flags)
    except OSError as exc:
        raise CredentialError("tty-required") from exc
    try:
        if not os.isatty(fd):
            raise CredentialError("tty-required")
        with os.fdopen(fd, "r+", encoding="utf-8", closefd=False) as tty:
            try:
                return prompt("Credential: ", stream=tty)
            except BaseException as exc:
                raise CredentialError("credential-input-failed") from exc
    finally:
        os.close(fd)
