from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import BinaryIO

RUNTIME_ERROR = "error: acp-failed"

class _TextOutput:
    def __init__(self, raw: BinaryIO) -> None: self.raw = raw
    def write(self, value: str) -> int: self.raw.write(value.encode()); return len(value)
    def flush(self) -> None: self.raw.flush()

class _Parser(argparse.ArgumentParser):
    def __init__(self, *args: object, output: BinaryIO, **kwargs: object) -> None:
        self._output = output; super().__init__(*args, **kwargs)
    def add_subparsers(self, **kwargs: object) -> argparse._SubParsersAction:
        kwargs["parser_class"] = lambda *args, **inner: _Parser(*args, output=self._output, **inner)
        return super().add_subparsers(**kwargs)
    def print_help(self, file: object = None) -> None:
        super().print_help(_TextOutput(self._output))
    def _print_message(self, message: str | None, file: object = None) -> None:
        if message:
            target = sys.stderr if file is sys.stderr else _TextOutput(self._output)
            target.write(message)
    def exit(self, status: int = 0, message: str | None = None) -> None:
        if message: self._print_message(message, sys.stderr if status else None)
        raise SystemExit(status)
    def error(self, message: str) -> None:
        if message == "argument seconds: must be an ASCII integer from 1 through 600":
            self.exit(2, message + "\n")
        super().error(message)


def _timeout_seconds(value: str) -> int:
    if not value or any(character < "0" or character > "9" for character in value):
        raise argparse.ArgumentTypeError("must be an ASCII integer from 1 through 600")
    significant = value.lstrip("0")
    if not significant or len(significant) > 3:
        raise argparse.ArgumentTypeError("must be an ASCII integer from 1 through 600")
    parsed = int(significant)
    if not 1 <= parsed <= 600:
        raise argparse.ArgumentTypeError("must be an ASCII integer from 1 through 600")
    return parsed


def _reserve_stdout() -> BinaryIO:
    fd = os.dup(1); os.set_inheritable(fd, False); os.dup2(2, 1); sys.stdout = sys.stderr
    return os.fdopen(fd, "wb", buffering=0)


def _parser(output: BinaryIO) -> argparse.ArgumentParser:
    parser = _Parser(prog="mimir acp", output=output)
    parser.add_argument("--profile", dest="proxy_profile")
    commands = parser.add_subparsers(dest="command")
    profiles = commands.add_parser("profile")
    profile_commands = profiles.add_subparsers(dest="profile_command", required=True)
    local = profile_commands.add_parser("add-local")
    local.add_argument("name"); local.add_argument("--home", required=True)
    ssh = profile_commands.add_parser("add-ssh")
    ssh.add_argument("name"); ssh.add_argument("--home", required=True)
    ssh.add_argument("--ssh-host", required=True); ssh.add_argument("--ssh-user", required=True)
    ssh.add_argument("--ssh-port", type=int, default=22)
    ssh.add_argument("--identity-file", required=True); ssh.add_argument("--known-hosts-file", required=True)
    profile_commands.add_parser("list")
    remove = profile_commands.add_parser("remove"); remove.add_argument("name")
    timeout = profile_commands.add_parser("set-timeout")
    timeout.add_argument("name"); timeout.add_argument("seconds", type=_timeout_seconds)
    credentials = commands.add_parser("credential")
    credential_commands = credentials.add_subparsers(dest="credential_command", required=True)
    for name in ("add", "replace", "remove"):
        command = credential_commands.add_parser(name); command.add_argument("name")
    credential_commands.add_parser("list")
    relay = commands.add_parser("relay"); relay.add_argument("--home", required=True)
    return parser


def _json(output: BinaryIO, value: object) -> None:
    output.write(json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n")

def _status(value: str) -> None:
    sys.stderr.write(value + "\n"); sys.stderr.flush()

def _error(code: str) -> int:
    _status(f"error: {code}"); return 1


def _profile_command(args: argparse.Namespace, output: BinaryIO) -> int:
    from .profiles import Profile, ProfileError, ProfileStore, RemoteProfile
    store = ProfileStore()
    try:
        if args.profile_command == "list":
            _json(output, {"version": 1, "profiles": [profile.name for profile in store.list()]}); return 0
        if args.profile_command == "remove":
            store.delete(args.name); _status("removed"); return 0
        if args.profile_command == "set-timeout":
            if store.get(args.name) is None:
                _status(f"profile '{args.name}' does not exist"); return 2
            store.set_timeout(args.name, args.seconds)
            _status(f"Set timeout for profile '{args.name}' to {args.seconds} seconds.")
            return 0
        if store.get(args.name) is not None:
            return _error("profile-already-exists")
        remote = None
        if args.profile_command == "add-ssh":
            remote = RemoteProfile(
                args.ssh_host, args.ssh_user, args.ssh_port,
                Path(args.identity_file), Path(args.known_hosts_file),
            )
        store.set(Profile(args.name, Path(args.home), remote)); _status("added"); return 0
    except ProfileError as exc: return _error(exc.code)
    except OSError: return _error("unsafe-profile-store")


def _credential_command(args: argparse.Namespace, output: BinaryIO) -> int:
    from .credentials import CredentialError, CredentialMutationUncertain, NativeCredentialStore, read_secret_from_tty
    from .profiles import ProfileError, ProfileStore
    try:
        profiles = ProfileStore()
        if args.credential_command == "list":
            store = NativeCredentialStore()
            values = [
                {"profile": profile.name, "stored": store.get(profile.name) is not None}
                for profile in profiles.list()
            ]
            _json(output, {"version": 1, "credentials": values}); return 0
        name = args.name
        if profiles.get(name) is None: return _error("profile-not-found")
        store = NativeCredentialStore()
        if args.credential_command == "remove":
            store.delete(name); _status("removed"); return 0
        store.require_available()
        existing = store.get(name)
        if args.credential_command == "add" and existing is not None:
            return _error("credential-already-exists")
        if args.credential_command == "replace" and existing is None:
            return _error("credential-not-found")
        secret = read_secret_from_tty(); store.set(name, secret); _status("added" if args.credential_command == "add" else "replaced"); return 0
    except CredentialMutationUncertain: _status("error: credential-mutation-uncertain"); return 3
    except CredentialError as exc: return _error(exc.code)
    except ProfileError as exc: return _error(exc.code)


def _proxy(args: argparse.Namespace, output: BinaryIO) -> int:
    from .credentials import CredentialError
    from .profiles import ProfileError, ProfileStore, selected_profile
    from .proxy import ProxyError, run_proxy
    from .ssh import SshError, run_remote_proxy
    try:
        name = selected_profile(args.proxy_profile); profile = ProfileStore().get(name)
        if profile is None: return _error("profile-not-found")
        if profile.remote is None: asyncio.run(run_proxy(name, output))
        else: asyncio.run(run_remote_proxy(name, output))
        return 0
    except ProfileError as exc: return _error(exc.code)
    except CredentialError as exc: return _error(exc.code)
    except (ProxyError, SshError, TimeoutError, ConnectionError, OSError): return _error("connection-failed")


def _dispatch(args: argparse.Namespace, output: BinaryIO) -> int:
    if args.command == "profile": return _profile_command(args, output)
    if args.command == "credential": return _credential_command(args, output)
    if args.command == "relay":
        from .relay import RelayError, run_relay
        try: asyncio.run(run_relay(Path(args.home), output)); return 0
        except (RelayError, TimeoutError, ConnectionError, OSError): return _error("connection-failed")
    return _proxy(args, output)


def main(argv: Sequence[str] | None = None) -> int:
    saved_fd = os.dup(1)
    saved_stdout = sys.stdout
    try: output = _reserve_stdout()
    except Exception:
        os.close(saved_fd); _status(RUNTIME_ERROR); return 1
    try:
        try: args = _parser(output).parse_args(list(argv or ()))
        except SystemExit as exc: return int(exc.code)
        try: return _dispatch(args, output)
        except (BrokenPipeError, ConnectionResetError): return 0
        except BaseException: return _error("acp-failed")
    finally:
        try: output.close()
        except Exception: pass
        os.dup2(saved_fd, 1); os.close(saved_fd); sys.stdout = saved_stdout
