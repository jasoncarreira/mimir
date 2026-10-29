"""Tracker adapters, imported lazily so one broken adapter cannot block the others."""

from __future__ import annotations

import importlib
from typing import Mapping

from ..config import ADAPTER_IDS, TrackerAdapterConfig
from ..transport import TrackerTransport
from .base import TrackerAdapter

_PACKAGE = "mimir_cultivate.tracker.adapters"


def build_adapter(
    name: str,
    config: TrackerAdapterConfig,
    env: Mapping[str, str],
    transport: TrackerTransport,
) -> TrackerAdapter:
    """Return the adapter for ``name``; unknown or unimportable names raise ValueError.

    Each adapter module exposes ``build(config, env, transport) -> TrackerAdapter``.
    """

    if name not in ADAPTER_IDS:
        raise ValueError(f"unknown tracker adapter: {name}")
    try:
        module = importlib.import_module(f"{_PACKAGE}.{name}")
    except ImportError as exc:
        raise ValueError(f"tracker adapter {name} is unavailable") from exc
    return module.build(config, env, transport)
