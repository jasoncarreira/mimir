from __future__ import annotations

from types import SimpleNamespace

import pytest
from keyring.errors import KeyringLocked, NoKeyringError

from mimir.acp import credentials
from mimir.acp.credentials import SERVICE, CredentialError, CredentialMutationUncertain, NativeCredentialStore


class Backend:
    priority = 1

    def __init__(self, value: str | None = None) -> None:
        self.value = value
        self.read_error: BaseException | None = None
        self.mutation_error: BaseException | None = None
        self.calls: list[tuple[object, ...]] = []

    def get_password(self, service: str, user: str) -> str | None:
        self.calls.append(("get", service, user))
        if self.read_error:
            raise self.read_error
        return self.value

    def set_password(self, service: str, user: str, value: str) -> None:
        self.calls.append(("set", service, user, value))
        if self.mutation_error:
            raise self.mutation_error
        self.value = value

    def delete_password(self, service: str, user: str) -> None:
        self.calls.append(("delete", service, user))
        if self.mutation_error:
            raise self.mutation_error
        self.value = None


def test_status_exact_present_and_missing_outputs() -> None:
    assert NativeCredentialStore(_backend=Backend("key")).status("default") is True
    assert NativeCredentialStore(_backend=Backend()).status("default") is False


def test_read_failure_is_definite_and_prevents_delete() -> None:
    backend = Backend("key")
    backend.read_error = RuntimeError("SECRET")
    store = NativeCredentialStore(_backend=backend)
    with pytest.raises(CredentialError, match="credential-read-failed"):
        store.delete("default")
    assert [call[0] for call in backend.calls] == ["get"]


def test_set_success_has_exact_output_and_no_readback() -> None:
    backend = Backend()
    NativeCredentialStore(_backend=backend).set("default", "key")
    assert backend.calls == [("set", SERVICE, "default", "key")]


def test_delete_absent_is_idempotent_without_dispatch() -> None:
    backend = Backend()
    store = NativeCredentialStore(_backend=backend)
    store.delete("default")
    store.delete("default")
    assert backend.calls == [("get", SERVICE, "default"), ("get", SERVICE, "default")]


def test_delete_present_dispatches_once() -> None:
    backend = Backend("key")
    NativeCredentialStore(_backend=backend).delete("default")
    assert backend.calls == [("get", SERVICE, "default"), ("delete", SERVICE, "default")]


@pytest.mark.parametrize("operation", ["set", "delete"])
def test_dispatched_mutation_exception_is_uncertain(operation: str) -> None:
    backend = Backend("key")
    backend.mutation_error = KeyboardInterrupt("SECRET")
    store = NativeCredentialStore(_backend=backend)
    with pytest.raises(CredentialMutationUncertain):
        getattr(store, operation)("default", "new") if operation == "set" else store.delete("default")


def test_backend_selection_and_unavailability_are_definite(monkeypatch: pytest.MonkeyPatch) -> None:
    native_type = type("Keyring", (Backend,), {"__module__": "keyring.backends.SecretService"})
    third_party = type("Keyring", (Backend,), {"__module__": "third.party"})()
    low = native_type(); low.priority = 1
    high = native_type(); high.priority = 10
    chainer_type = type("ChainerBackend", (), {"__module__": "keyring.backends.chainer"})
    monkeypatch.setitem(__import__("sys").modules, "keyring", SimpleNamespace(get_keyring=lambda: chainer_type()))
    chainer_type.backends = [third_party, low, high]
    assert credentials._production_backend() is high
    monkeypatch.setitem(__import__("sys").modules, "keyring", SimpleNamespace(get_keyring=lambda: third_party))
    with pytest.raises(CredentialError, match="secure-store-unavailable"):
        credentials._production_backend()


@pytest.mark.parametrize("operation", ["get", "delete"])
@pytest.mark.parametrize("error", [KeyringLocked("locked"), NoKeyringError("missing")])
def test_locked_or_unavailable_native_store_is_not_a_read_failure(operation: str, error: BaseException) -> None:
    backend = Backend("key")
    backend.read_error = error
    store = NativeCredentialStore(_backend=backend)
    with pytest.raises(CredentialError, match="secure-store-unavailable"):
        getattr(store, operation)("default")
    assert [call[0] for call in backend.calls] == ["get"]


@pytest.mark.parametrize("operation", ["get", "delete"])
def test_other_native_read_errors_remain_credential_read_failed(operation: str) -> None:
    backend = Backend("key")
    backend.read_error = RuntimeError("private")
    store = NativeCredentialStore(_backend=backend)
    with pytest.raises(CredentialError, match="credential-read-failed"):
        getattr(store, operation)("default")


def test_set_requires_tty_and_maps_input_failures_before_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(credentials.os, "open", lambda *args: (_ for _ in ()).throw(OSError()))
    with pytest.raises(CredentialError, match="tty-required"):
        credentials.read_secret_from_tty()

    monkeypatch.setattr(credentials.os, "open", lambda *args: 99)
    monkeypatch.setattr(credentials.os, "isatty", lambda fd: True)
    monkeypatch.setattr(credentials.os, "close", lambda fd: None)
    class Tty:
        def __enter__(self) -> object: return self
        def __exit__(self, *args: object) -> None: return None
    monkeypatch.setattr(credentials.os, "fdopen", lambda *args, **kwargs: Tty())
    with pytest.raises(CredentialError, match="credential-input-failed"):
        credentials.read_secret_from_tty(lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))
