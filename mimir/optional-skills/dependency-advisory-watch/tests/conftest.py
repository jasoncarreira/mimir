"""Test fixtures for dependency-advisory-watch scanner."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import scanner


@pytest.fixture
def tmp_root(tmp_path):
    """Create a temporary root directory for tests."""
    return tmp_path


@pytest.fixture
def uv_lock_content():
    """Sample uv.lock content for testing."""
    return '''version = 1
revision = 1
requires-python = ">=3.11"

[[package]]
name = "test-package"
version = "1.0.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "vulnerable-package"
version = "2.3.4"
source = { registry = "https://pypi.org/simple" }
'''


@pytest.fixture
def package_lock_content():
    """Sample package-lock.json content for testing."""
    return json.dumps({
        "name": "test-project",
        "version": "1.0.0",
        "lockfileVersion": 3,
        "packages": {
            "node_modules/test-pkg": {
                "version": "1.0.0",
                "name": "test-pkg"
            },
            "node_modules/vuln-pkg": {
                "version": "2.3.4",
                "name": "vuln-pkg"
            }
        }
    })


@pytest.fixture
def captured_events(monkeypatch):
    """Capture emitted events."""
    events: list[dict] = []

    def mock_emit(event: dict) -> None:
        events.append(event)

    monkeypatch.setattr(scanner, "_emit", mock_emit)
    return events
