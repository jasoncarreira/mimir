from __future__ import annotations

import os
import pty
import select
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from keyring.errors import KeyringLocked, NoKeyringError

from mimir.acp import credentials
from mimir.acp.credentials import SERVICE, CredentialError, CredentialMutationUncertain, NativeCredentialStore


def _intercept_tty(monkeypatch: pytest.MonkeyPatch, answer: object) -> None:
    """Redirect only "/dev/tty". credentials.os is the os module itself, so an
    unconditional patch also answers every unrelated open in the process."""
    real_open = os.open

    def opener(path: str, *args: object, **kwargs: object) -> int:
        if path != "/dev/tty":
            return real_open(path, *args, **kwargs)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(credentials.os, "open", opener)


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
    _intercept_tty(monkeypatch, OSError())
    with pytest.raises(CredentialError, match="tty-required"):
        credentials.read_secret_from_tty()

    master, slave = pty.openpty()
    _intercept_tty(monkeypatch, slave)
    try:
        with pytest.raises(CredentialError, match="credential-input-failed"):
            credentials.read_secret_from_tty(lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))
    finally:
        os.close(master)


def test_read_secret_from_tty_prompts_and_reads_over_a_real_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """A terminal is not seekable. Drive one for real: a stubbed file object cannot
    tell us whether the reader can open the device it exists to read."""
    master, slave = pty.openpty()
    _intercept_tty(monkeypatch, slave)
    seen: list[bytes] = []

    def prompt(text: str, stream: object) -> str:
        stream.write(text); stream.flush()
        # The echo of the line already written and the prompt itself do not
        # necessarily arrive in one chunk, so drain until the prompt shows up.
        deadline = time.monotonic() + 5.0
        while b"Credential: " not in b"".join(seen) and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                seen.append(os.read(master, 256))
        return stream.readline().rstrip("\n")

    os.write(master, b"top-secret\n")
    try:
        assert credentials.read_secret_from_tty(prompt) == "top-secret"
    finally:
        os.close(master)
    assert b"Credential: " in b"".join(seen)


def test_default_prompt_reads_the_controlling_terminal_and_suppresses_echo() -> None:
    """The reader's default prompt is getpass, which opens /dev/tty for itself: handing
    it a stream proves nothing about which descriptor supplies the input. Give a child
    a controlling terminal we own, and read that terminal back."""
    program = (
        "import fcntl, termios\n"
        "fcntl.ioctl(0, termios.TIOCSCTTY, 0)\n"
        "from mimir.acp.credentials import read_secret_from_tty\n"
        "print('GOT:' + read_secret_from_tty(), flush=True)\n"
    )
    master, slave = pty.openpty()
    child = subprocess.Popen(
        [sys.executable, "-c", program],
        stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
    )
    os.close(slave)

    seen, sent, deadline = b"", False, time.monotonic() + 30.0
    try:
        while b"GOT:" not in seen and time.monotonic() < deadline:
            if select.select([master], [], [], 0.2)[0]:
                try:
                    chunk = os.read(master, 1024)
                except OSError:
                    break
                if not chunk:
                    break
                seen += chunk
            if not sent and b"Credential: " in seen:
                os.write(master, b"hunter2\n"); sent = True
    finally:
        os.close(master)
        child.terminate()
        child.wait(timeout=10)

    assert b"Credential: " in seen, seen
    assert b"GOT:hunter2" in seen, seen
    # Everything the terminal showed before the answer was printed. The typed secret
    # must not be among it, or getpass failed to turn echo off.
    assert b"hunter2" not in seen.split(b"GOT:")[0], seen
