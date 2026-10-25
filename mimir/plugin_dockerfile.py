"""Inert, build-time Docker contributions from installed plugin distributions."""

from __future__ import annotations

import argparse
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from packaging.utils import canonicalize_name

from .plugins.discovery import PluginCandidate, PluginDiscoveryError, discover_plugins


_CULTIVATE_PACKAGE = "mimir-cultivate"
_CULTIVATE_SCRIPTS = ("base.sh", "users.sh", "executor.sh", "controller.sh", "bootstrap.sh", "service.sh")


def has_trusted_cultivate(candidates: tuple[PluginCandidate, ...]) -> bool:
    """The privileged image slot belongs to the first-party distribution only."""
    return any(c.name == "cultivate" and canonicalize_name(c.distribution) == _CULTIVATE_PACKAGE
               for c in candidates)


def cultivate_scripts(candidate: PluginCandidate) -> dict[str, str]:
    """Read only inventoried resources from the selected first-party distribution."""
    if not has_trusted_cultivate((candidate,)):
        raise ValueError("trusted Docker scripts require the Cultivate distribution")
    dist = candidate.entry_point.dist
    if dist is None or dist.files is None:
        raise ValueError("Cultivate Docker resource inventory is missing")
    inventory = {str(path): path for path in dist.files}
    scripts = {}
    for name in _CULTIVATE_SCRIPTS:
        relative = f"mimir_cultivate/docker/{name}"
        if relative not in inventory:
            raise ValueError(f"Cultivate Docker resource is missing: {relative}")
        path = Path(dist.locate_file(inventory[relative]))
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Cultivate Docker resource is not a regular file: {relative}")
        scripts[name] = path.read_text(encoding="utf-8")
    return scripts


@dataclass(frozen=True)
class Service:
    plugin: str
    name: str
    run: Path


def select_plugins(*, all_installed: bool = False, enable: str | None = None) -> tuple[PluginCandidate, ...]:
    """Select metadata without loading any entry point, failing on invalid selections."""
    candidates = discover_plugins(enable=enable)
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
            if identifier in names or identifier in {"mimir", "watchdog", "worklink-execd", "mimir-plugin-reset"}:
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
    if stage == "services" and lines:
        lines.append(
            "COPY .mimir-scaffold/plugin-services/ /usr/local/libexec/mimir-plugin-services/\n"
            "COPY .mimir-scaffold/s6-rc.d/ /etc/s6-overlay/s6-rc.d/\n"
            "RUN chown -R root:root /usr/local/libexec/mimir-plugin-services /etc/s6-overlay/s6-rc.d \\\n"
            " && chmod -R go-w /usr/local/libexec/mimir-plugin-services /etc/s6-overlay/s6-rc.d"
        )
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
        # Same identity contract as deploy/s6-overlay run scripts (#2004): s6-setuidgid
        # keeps the supervisor environment, so set the dropped user's identity explicitly.
        wrapper.write_text(
            "#!/bin/sh\nexec s6-setuidgid mimir env HOME=/home/mimir USER=mimir LOGNAME=mimir "
            f"/usr/local/libexec/mimir-plugin-services/{service.plugin}/{service.name}/run\n"
        )
        wrapper.chmod(0o755)
        (bundle / identifier).touch()


def verify_pins(pins: tuple[str, ...], candidates: tuple[PluginCandidate, ...]) -> None:
    actual = pinned_packages(candidates)
    if tuple(sorted(pins)) != tuple(sorted(actual)):
        raise ValueError(f"selected provider packages differ: requested {pins}, installed {actual}")


def assemble_image(*, pins: tuple[str, ...], enable: str, all_installed: bool,
                   destination: Path, scripts: Path) -> None:
    """Verify the pinned build inputs, then assemble only the selected services."""
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*==[A-Za-z0-9][A-Za-z0-9_.!+~-]*", pin)
           for pin in pins):
        raise ValueError("plugin package pins must be exact distribution==version pairs")
    candidates = discover_plugins(enable=enable)
    pinned = tuple(c for c in candidates if any(
        canonicalize_name(c.distribution) == canonicalize_name(pin.split("==")[0]) for pin in pins
    ))
    verify_pins(pins, pinned)
    selected = tuple(c for c in candidates if all_installed or c.enabled)
    missing = [c.name for c in selected if c not in pinned]
    if missing:
        raise ValueError(f"selected plugin(s) missing pinned distribution: {', '.join(missing)}")
    for candidate in selected:
        if candidate.diagnostic or candidate.manifest is None:
            raise PluginDiscoveryError(f"plugin {candidate.name}: {candidate.diagnostic or 'missing manifest'}")
    assemble_services(selected, destination, scripts)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packages", required=True, help="comma-separated pinned provider distributions")
    parser.add_argument("--enable", default="", help="build-time MIMIR_PLUGINS selection")
    parser.add_argument("--all-installed", choices=("0", "1"), default="0")
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--scripts", type=Path, required=True)
    args = parser.parse_args()
    pins = tuple(filter(None, args.packages.split(",")))
    assemble_image(pins=pins, enable=args.enable, all_installed=args.all_installed == "1",
                   destination=args.destination, scripts=args.scripts)


if __name__ == "__main__":
    main()
