"""Check the canonical Dockerfile's base-image registry and override."""

from __future__ import annotations

from pathlib import Path

from mimir.scaffold_docker import DEFAULT_BASE_IMAGE


def test_root_dockerfile_uses_mirrored_base_image() -> None:
    text = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
    instructions = [
        line for line in text.splitlines() if line.startswith(("ARG ", "FROM "))
    ]
    assert DEFAULT_BASE_IMAGE == "public.ecr.aws/docker/library/python:3.11-slim"
    assert instructions[0] == f"ARG BASE_IMAGE={DEFAULT_BASE_IMAGE}"
    assert instructions[1] == "FROM ${BASE_IMAGE} AS provenance-validation"
    assert not any(line.startswith("FROM python:") for line in instructions)
