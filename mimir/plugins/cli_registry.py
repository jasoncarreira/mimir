"""Argparse command registry shared by the core CLI and selected providers."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from typing import Any

from .registry import CliCommand, PolicyRegistrationError


class CommandRegistry:
    def __init__(self) -> None:
        self._commands: dict[str, CliCommand] = {}

    @property
    def commands(self) -> Mapping[str, CliCommand]:
        return self._commands.copy()

    def register(self, command: CliCommand) -> None:
        if command.name in self._commands:
            previous = self._commands[command.name]
            raise PolicyRegistrationError(
                f"CLI command {command.name!r} from {command.owner} conflicts with {previous.owner}"
            )
        self._commands[command.name] = command

    def add_core(self, name: str, parser: argparse.ArgumentParser,
                 dispatch: Callable[[argparse.Namespace], Any]) -> None:
        self.register(CliCommand("core", name, lambda _sub: parser, dispatch))

    def add_selected(self, commands: Mapping[str, CliCommand],
                     sub: argparse._SubParsersAction) -> None:
        for command in commands.values():
            self.register(command)
            if command.owner == "core":
                continue
            before = set(sub.choices)
            command.configure(sub)
            if set(sub.choices) - before != {command.name}:
                raise PolicyRegistrationError(
                    f"{command.owner}: CLI parser must add exactly {command.name!r}"
                )

    def dispatch(self, args: argparse.Namespace) -> Any:
        return self._commands[args.command].dispatch(args)
