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


def _reserve_stdout() -> BinaryIO:
    fd = os.dup(1); os.set_inheritable(fd, False); os.dup2(2, 1); sys.stdout = sys.stderr
    return os.fdopen(fd, "wb", buffering=0)


def _parser(output: BinaryIO) -> argparse.ArgumentParser:
    parser = _Parser(prog="mimir acp", output=output)
    parser.add_argument("--profile", dest="proxy_profile")
    commands = parser.add_subparsers(dest="command")
    profiles = commands.add_parser("profile")
    profile_commands = profiles.add_subparsers(dest="profile_command", required=True)
    profile_commands.add_parser("list")
    show = profile_commands.add_parser("show"); show.add_argument("name")
    setp = profile_commands.add_parser("set"); setp.add_argument("name"); setp.add_argument("--home", required=True)
    setp.add_argument("--ssh-host"); setp.add_argument("--ssh-user"); setp.add_argument("--ssh-port", type=int, default=22)
    setp.add_argument("--identity-file"); setp.add_argument("--known-hosts-file")
    delete = profile_commands.add_parser("delete"); delete.add_argument("name")
    credentials = commands.add_parser("credential")
    credential_commands = credentials.add_subparsers(dest="credential_command", required=True)
    for name in ("set", "delete", "status"):
        command = credential_commands.add_parser(name); command.add_argument("--profile")
    relay = commands.add_parser("relay"); relay.add_argument("--home", required=True)
    return parser


def _json(output: BinaryIO, value: object) -> None:
    output.write(json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n")

def _status(value: str) -> None:
    sys.stderr.write(value + "\n"); sys.stderr.flush()

def _error(code: str) -> int:
    _status(f"error: {code}"); return 1


def _profile_command(args: argparse.Namespace, output: BinaryIO) -> int:
    from .profiles import Profile, ProfileError, ProfileStore, RemoteProfile, profile_json
    store = ProfileStore()
    try:
        if args.profile_command == "list":
            _json(output, {"version": 1, "profiles": [profile.name for profile in store.list()]}); return 0
        if args.profile_command == "show":
            profile = store.get(args.name)
            if profile is None: return _error("profile-not-found")
            _json(output, profile_json(profile)); return 0
        if args.profile_command == "delete":
            store.delete(args.name); _status("deleted"); return 0
        remote_values = (args.ssh_host, args.ssh_user, args.identity_file, args.known_hosts_file)
        any_remote = any(value is not None for value in remote_values) or args.ssh_port != 22
        if any_remote and any(value is None for value in remote_values): raise ProfileError()
        remote = None if not any_remote else RemoteProfile(args.ssh_host, args.ssh_user, args.ssh_port, Path(args.identity_file), Path(args.known_hosts_file))
        store.set(Profile(args.name, Path(args.home), remote)); _status("updated"); return 0
    except ProfileError as exc: return _error(exc.code)
    except OSError: return _error("unsafe-profile-store")


def _credential_command(args: argparse.Namespace) -> int:
    from .credentials import CredentialError, CredentialMutationUncertain, NativeCredentialStore, read_secret_from_tty
    from .profiles import ProfileError, ProfileStore, selected_profile
    try:
        name = selected_profile(args.profile)
        if ProfileStore().get(name) is None: return _error("profile-not-found")
        store = NativeCredentialStore()
        if args.credential_command == "status": _status("stored" if store.get(name) is not None else "missing"); return 0
        if args.credential_command == "delete": store.delete(name); _status("deleted"); return 0
        secret = read_secret_from_tty(); store.set(name, secret); _status("updated"); return 0
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
    if args.command == "credential": return _credential_command(args)
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
