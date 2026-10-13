"""Minimal installed provider used by the plugin integration tests."""

from __future__ import annotations

from dataclasses import dataclass

from aiohttp import web
from langchain_core.tools import StructuredTool

from mimir.dashboard_extensions import DashboardExtensionManifest
from mimir.poller_hooks import PollerHooks
from mimir.tool_descriptors import ResultOriginKind


def _tool():
    def example_read() -> str:
        """Read the example provider's public marker."""
        return "example-result"

    return StructuredTool.from_function(example_read, name="example_read")


def _hooks() -> PollerHooks:
    return PollerHooks(recovery_relevance=lambda _poller: lambda _event: True)


def _configure_cli(sub) -> None:
    parser = sub.add_parser("example-hello", help="Print the example greeting")
    parser.add_argument("--name", default="operator")


def _dispatch_cli(args) -> int:
    print(f"hello, {args.name}")
    return 0


async def _status(_request: web.Request) -> web.Response:
    return web.json_response({"provider": "example"})


def _applies(_auth, _event) -> bool:
    return True


def _render(_auth, _event, reader) -> str:
    return "Example memory: " + reader("memory/core/00-identity.md")


@dataclass(frozen=True)
class ExampleState:
    greeting: str


class ExampleProvider:
    def register(self, registrar) -> None:
        registrar.register_tool(
            name="example_read", factory=_tool, flow="source", tier="scope-contained",
            operation_class="open", result_domain="example-data",
            result_origin=ResultOriginKind.EXTERNAL, sink_category=None,
            sink_destination=None, sink_target_extractor=None, sink_payload_extractor=None,
            writes_code_or_state=False, profiles=("example-reader",), read_backend=True,
        )
        registrar.register_capability("example_read")
        registrar.register_authority_profile("example-reader", capabilities=("example_read",))
        registrar.register_poller_hook("example-hooks", _hooks)
        registrar.register_scheduler_job("example-tick", "example-tick", self.tick)
        registrar.register_cli_command("example-hello", _configure_cli, _dispatch_cli)
        registrar.register_route("GET", "/api/v1/plugins/example/status", _status)
        registrar.register_dashboard_entry("example-page", DashboardExtensionManifest(
            id="example-page", route_path="/example", label="Example", bundle="example.v1.js",
        ))
        registrar.register_prompt_provider("example_memory", "channel_memory", 4096, _applies, _render)
        registrar.register_state_slot(ExampleState)

    async def tick(self) -> None:
        return None


provider = ExampleProvider()
