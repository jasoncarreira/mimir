#!/bin/sh
set -eu

# Isolated, locked core installation: the four suites below deliberately install otel.
uv run --isolated --locked --no-default-groups --no-dev python - <<'PY'
import asyncio
import importlib.util
import logging
import os
import tempfile
from dataclasses import replace

for name in ("opentelemetry.sdk", "opentelemetry.exporter"):
    try:
        spec = importlib.util.find_spec(name)
    except ModuleNotFoundError:
        spec = None
    assert spec is None, f"no-extra smoke installed {name}"

from mimir import server, runtime
from mimir.channel_registry import ChannelRegistry
from mimir.config import Config
from mimir.dispatcher import Dispatcher
from mimir.scheduler import Scheduler

class Warnings(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())

async def boot(config):
    core = runtime.create_core_services(config)
    dispatcher = Dispatcher(config, resolver=core.identity_resolver)
    scheduler = Scheduler(config.home / "scheduler.yaml", dispatcher.enqueue, home=config.home)
    adapters = runtime.RuntimeAdapters(
        dispatcher=dispatcher, scheduler=scheduler, channels=ChannelRegistry(),
        pairing_notifier=None,
        spawn_background_task=lambda coro, name: asyncio.create_task(coro, name=name),
    )
    bundle = await runtime.create_agent_runtime(config, core, adapters)
    try:
        assert bundle.trace_controller is None
        assert bundle.agent._trace_controller is None
    finally:
        await bundle.aclose()

async def main():
    with tempfile.TemporaryDirectory() as directory:
        for key in list(os.environ):
            if key.startswith("MIMIR_"):
                del os.environ[key]
        os.environ["MIMIR_HOME"] = directory
        os.environ["MIMIR_PLUGINS"] = ""
        config = Config.from_env()
        assert not config.tracing_enabled
        warnings = Warnings()
        logger = logging.getLogger("mimir.tracing")
        logger.addHandler(warnings)
        try:
            await boot(config)
            assert warnings.messages == [], warnings.messages
            secret = "no-extra-credential-canary"
            # The valid endpoint reaches the missing-SDK branch; a sensitive
            # service name must not escape into the warning on failure.
            traced = replace(config, tracing_enabled=True,
                             otlp_endpoint="http://localhost:4318",
                             otel_service_name=secret)
            await boot(traced)
            assert warnings.messages == ["Tracing disabled: OTLP exporter unavailable."], warnings.messages
            assert secret not in "\n".join(warnings.messages)
        finally:
            logger.removeHandler(warnings)

asyncio.run(main())
PY

env -u MIMIR_ACCESS_CONTROL_ENFORCED uv run --extra dev --extra bench --extra otel pytest -q -n 6
MIMIR_ACCESS_CONTROL_ENFORCED=1 uv run --extra dev --extra bench --extra otel pytest -q -n 6
env -u MIMIR_ACCESS_CONTROL_ENFORCED MIMIR_PLUGINS=cultivate uv run --extra dev --extra bench --extra otel pytest -q -n 6 tests plugins/cultivate/tests
MIMIR_ACCESS_CONTROL_ENFORCED=1 MIMIR_PLUGINS=cultivate uv run --extra dev --extra bench --extra otel pytest -q -n 6 tests plugins/cultivate/tests
