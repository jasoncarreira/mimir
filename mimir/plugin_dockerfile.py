"""Inert, build-time Docker contributions from installed plugin distributions."""

from __future__ import annotations

import argparse
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from packaging.utils import canonicalize_name

from .plugins.discovery import PluginCandidate, PluginDiscoveryError, discover_plugins


@dataclass(frozen=True)
class Service:
    plugin: str
    name: str
    run: Path


def select_plugins(*, all_installed: bool = False) -> tuple[PluginCandidate, ...]:
    """Select metadata without loading any entry point, failing on invalid selections."""
    candidates = discover_plugins()
    selected = tuple(c for c in candidates if all_installed or c.enabled)
    for candidate in selected:
        if candidate.diagnostic or candidate.manifest is None:
            raise PluginDiscoveryError(
                f"plugin {candidate.name!r} from {candidate.distribution}: "
                f"{candidate.diagnostic or 'missing manifest'}"
            )
    return selected


def pinned_packages(candidates: tuple[PluginCandidate, ...]) -> tuple[str, ...]:
    packages = []
    for candidate in candidates:
        dist = candidate.entry_point.dist
        if dist is None or not dist.version:
            raise ValueError(f"plugin {candidate.name}: missing installed distribution version")
        packages.append(f"{candidate.distribution}=={dist.version}")
    return tuple(packages)


def services(candidates: tuple[PluginCandidate, ...]) -> tuple[Service, ...]:
    result = []
    names = set()
    for candidate in candidates:
        assert candidate.manifest is not None
        dist = candidate.entry_point.dist
        assert dist is not None and dist.files is not None
        manifests = [p for p in dist.files if p.name == "mimir-plugin.toml"]
        if len(manifests) != 1:
            raise ValueError(f"plugin {candidate.name}: missing manifest inventory")
        root = Path(dist.locate_file(manifests[0])).parent
        for relative in candidate.manifest.dockerfile.get("s6_services", ()):
            name = Path(relative).name
            if not re.fullmatch(r"[a-z][a-z0-9-]*", name):
                raise ValueError(f"plugin {candidate.name}: invalid service name {name!r}")
            identifier = f"{candidate.name}-{name}"
            if identifier in names or identifier in {"mimir", "watchdog", "worklink-execd"}:
                raise ValueError(f"duplicate s6 service {identifier}")
            names.add(identifier)
            result.append(Service(candidate.name, name, root / relative / "run"))
    return tuple(result)


def render_stage(candidates: tuple[PluginCandidate, ...], stage: str) -> str:
    """Return deterministic Docker instructions for one contribution stage."""
    if stage not in {"base", "build", "runtime", "services"}:
        raise ValueError(f"unknown Docker stage: {stage}")
    lines = []
    for candidate in candidates:
        assert candidate.manifest is not None
        spec = candidate.manifest.dockerfile
        fragment = []
        if stage == "base" and spec.get("system_packages"):
            fragment.append("RUN apt-get update && apt-get install -y --no-install-recommends " +
                            " ".join(spec["system_packages"]) + " && rm -rf /var/lib/apt/lists/*")
        if stage == "build":
            fragment.extend(f'ARG {key}="{value}"' for key, value in sorted(spec.get("build_args", {}).items()))
            fragment.extend(f"RUN useradd --create-home {user}" for user in spec.get("users", ()))
            fragment.extend(f"RUN mkdir -p {directory}" for directory in spec.get("directories", ()))
            fragment.extend(f"RUN {command}" for command in spec.get("root_run", ()))
        if stage == "services":
            fragment.extend(f"# {service.plugin}-{service.name}: {service.run.parent.name}/run"
                            for service in services((candidate,)))
        if fragment:
            lines.append(f"# --- plugin {candidate.name} ---\n" + "\n".join(fragment))
    return "\n\n".join(lines)


def assemble_services(candidates: tuple[PluginCandidate, ...], destination: Path, scripts: Path) -> None:
    """Write root-owned s6 definitions at image build time; never at runtime."""
    selected = services(candidates)
    bundle = destination / "user" / "contents.d"
    bundle.mkdir(parents=True, exist_ok=True)
    for service in selected:
        identifier = f"{service.plugin}-{service.name}"
        target = scripts / service.plugin / service.name
        target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(service.run, target / "run")
        (target / "run").chmod(0o755)
        definition = destination / identifier
        definition.mkdir(parents=True, exist_ok=True)
        (definition / "type").write_text("longrun\n")
        wrapper = definition / "run"
        wrapper.write_text(f"#!/bin/sh\nexec s6-setuidgid mimir /usr/local/libexec/mimir-plugin-services/{service.plugin}/{service.name}/run\n")
        wrapper.chmod(0o755)
        (bundle / identifier).touch()


def verify_pins(pins: tuple[str, ...], candidates: tuple[PluginCandidate, ...]) -> None:
    actual = pinned_packages(candidates)
    if tuple(sorted(pins)) != tuple(sorted(actual)):
        raise ValueError(f"selected provider packages differ: requested {pins}, installed {actual}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packages", required=True, help="comma-separated pinned provider distributions")
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--scripts", type=Path, required=True)
    args = parser.parse_args()
    pins = tuple(filter(None, args.packages.split(",")))
    selected = tuple(c for c in discover_plugins() if any(
        canonicalize_name(c.distribution) == canonicalize_name(p.split("==")[0])
        for p in pins
    ))
    for candidate in selected:
        if candidate.diagnostic or candidate.manifest is None:
            raise PluginDiscoveryError(f"plugin {candidate.name}: {candidate.diagnostic}")
    verify_pins(pins, selected)
    assemble_services(selected, args.destination, args.scripts)


if __name__ == "__main__":
    main()
