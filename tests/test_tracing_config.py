"""Configuration and installation contracts for optional tracing."""

from __future__ import annotations

import tomllib
from pathlib import Path

from mimir.config import Config
from tests.isolation_fixtures import _HOST_ONLY_ENV


ROOT = Path(__file__).resolve().parent.parent
SETTINGS = {
    "MIMIR_TRACING_ENABLED",
    "MIMIR_OTLP_ENDPOINT",
    "MIMIR_OTEL_SERVICE_NAME",
    "MIMIR_TRACING_CAPTURE_CONTENT",
}


def test_tracing_defaults_and_host_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    for name in SETTINGS:
        monkeypatch.delenv(name, raising=False)
    cfg = Config.from_env()
    assert (cfg.tracing_enabled, cfg.otlp_endpoint, cfg.otel_service_name,
            cfg.tracing_capture_content) == (False, None, "mimir", False)
    assert SETTINGS <= _HOST_ONLY_ENV


def test_tracing_home_dotenv_and_process_precedence(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "MIMIR_TRACING_ENABLED=true\nMIMIR_OTLP_ENDPOINT=https://home.example\n"
        "MIMIR_OTEL_SERVICE_NAME=home-service\n"
        "MIMIR_TRACING_CAPTURE_CONTENT=yes\n", encoding="utf-8",
    )
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    for name in SETTINGS:
        monkeypatch.delenv(name, raising=False)
    cfg = Config.from_env()
    assert (cfg.tracing_enabled, cfg.otlp_endpoint, cfg.otel_service_name,
            cfg.tracing_capture_content) == (True, "https://home.example", "home-service", True)

    monkeypatch.setenv("MIMIR_OTLP_ENDPOINT", "https://process.example")
    monkeypatch.setenv("MIMIR_TRACING_ENABLED", "false")
    monkeypatch.setenv("MIMIR_TRACING_CAPTURE_CONTENT", "0")
    cfg = Config.from_env()
    assert (cfg.tracing_enabled, cfg.otlp_endpoint, cfg.tracing_capture_content) == (
        False, "https://process.example", False,
    )


def test_tracing_extras_and_dev_install_contract():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pins = {
        "opentelemetry-api==1.45.1",
        "opentelemetry-sdk==1.45.1",
        "opentelemetry-exporter-otlp-proto-http==1.45.1",
    }
    extras = project["project"]["optional-dependencies"]
    assert pins == set(extras["otel"])
    assert pins.isdisjoint(project["project"]["dependencies"])
    for dev in (extras["dev"], project["dependency-groups"]["dev"]):
        assert pins - {"opentelemetry-api==1.45.1"} <= set(dev)
        assert "mimir-cultivate" in dev
    assert project["tool"]["uv"]["sources"]["mimir-cultivate"] == {"workspace": True}
