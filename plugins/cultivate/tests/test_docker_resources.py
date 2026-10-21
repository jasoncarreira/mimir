from __future__ import annotations

import importlib.metadata
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from mimir.plugin_dockerfile import cultivate_scripts, has_trusted_cultivate, services
from mimir.scaffold_docker import render_dockerfile, scaffold


ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "plugins/cultivate/mimir_cultivate/docker"
SCRIPT_NAMES = ("base.sh", "users.sh", "executor.sh", "bootstrap.sh", "service.sh")


def _candidate(*, name: str = "cultivate", distribution: str = "mimir-cultivate",
               missing: str | None = None):
    files = [Path("mimir-plugin.toml")]
    files.extend(Path("mimir_cultivate/docker") / path.name for path in SCRIPTS.iterdir()
                 if path.is_file() and path.name != missing)
    dist = SimpleNamespace(version="0.9.10", files=files,
                           locate_file=lambda path: ROOT / "plugins/cultivate" / path)
    return SimpleNamespace(name=name, distribution=distribution,
                           manifest=SimpleNamespace(dockerfile={}),
                           entry_point=SimpleNamespace(dist=dist))


def test_selected_cultivate_uses_only_inventoried_first_party_scripts() -> None:
    candidate = _candidate()
    assert has_trusted_cultivate((candidate,))
    scripts = cultivate_scripts(candidate)
    assert set(scripts) == {"base.sh", "users.sh", "executor.sh", "bootstrap.sh", "service.sh"}
    assert 'origin "$MIMIR_EXECUTOR_COMMIT"' in scripts["executor.sh"]
    assert 'mimir_cultivate.worklink.worker_exec' in scripts["service.sh"]
    assert not services((candidate,)), "the reserved worker service is not a generic manifest service"


def test_built_wheel_and_installed_inventory_contain_exact_scripts(tmp_path: Path) -> None:
    subprocess.run(["uv", "build", "--package", "mimir-cultivate", "--wheel",
                    "--out-dir", str(tmp_path)], cwd=ROOT, check=True, capture_output=True, text=True)
    wheel, = tmp_path.glob("mimir_cultivate-*.whl")
    with ZipFile(wheel) as archive:
        for name in SCRIPT_NAMES:
            relative = f"mimir_cultivate/docker/{name}"
            assert archive.read(relative) == (SCRIPTS / name).read_bytes()
    target = tmp_path / "installed"
    subprocess.run(["uv", "pip", "install", "--target", str(target), "--no-deps", str(wheel)],
                   check=True, capture_output=True, text=True)
    dist, = (dist for dist in importlib.metadata.distributions(path=[str(target)])
             if dist.metadata["Name"] == "mimir-cultivate")
    candidate = SimpleNamespace(name="cultivate", distribution="mimir-cultivate",
                                entry_point=SimpleNamespace(dist=dist))
    assert cultivate_scripts(candidate) == {name: (SCRIPTS / name).read_text() for name in SCRIPT_NAMES}
    for name in SCRIPT_NAMES:
        assert dist.locate_file(f"mimir_cultivate/docker/{name}").read_bytes() == (SCRIPTS / name).read_bytes()


def test_missing_resource_or_untrusted_provider_cannot_get_privileged_slot() -> None:
    with pytest.raises(ValueError, match="resource is missing"):
        cultivate_scripts(_candidate(missing="executor.sh"))
    for candidate in (_candidate(distribution="third-party"), _candidate(name="impostor")):
        assert not has_trusted_cultivate((candidate,))
        with pytest.raises(ValueError, match="Cultivate distribution"):
            cultivate_scripts(candidate)
        assert "/opt/cultivate-docker" not in render_dockerfile([], mode="pypi", plugins=(candidate,))


@pytest.mark.parametrize("mode", ["workspace", "pypi"])
def test_selected_scaffold_composes_fixed_worker_and_supervisor(mode: str) -> None:
    text = render_dockerfile([], mode=mode, plugins=(_candidate(),))
    assert "RUN sh /opt/cultivate-docker/users.sh" in text
    assert 'RUN groupadd --gid "${USER_GID}" mimir' not in text
    assert 'ARG MIMIR_EXECUTOR_COMMIT' in text
    assert 'MIMIR_EXTRAS="$MIMIR_EXTRAS" sh /opt/cultivate-docker/executor.sh' in text
    assert 'mimir-cultivate==0.9.10' in text
    assert "COPY .mimir-scaffold/s6-rc.d/ /etc/s6-overlay/s6-rc.d/" in text
    assert 'ENTRYPOINT ["/init"]' in text
    assert 'ARG MIMIR_CODING_ENABLED' not in text


@pytest.mark.parametrize("mode", ["workspace", "pypi"])
def test_selected_scaffold_writes_worker_resources(tmp_path: Path, monkeypatch, mode: str) -> None:
    monkeypatch.setattr("mimir.scaffold_docker.select_plugins", lambda **kwargs: (_candidate(),))
    scaffold(tmp_path, mode=mode)
    context = tmp_path / ".mimir-scaffold"
    assert (context / "cultivate-docker/executor.sh").read_text() == (
        SCRIPTS / "executor.sh").read_text()
    assert (context / "s6-rc.d/user/contents.d/worklink-execd").is_file()
    assert (context / "s6-rc.d/worklink-execd/run").read_text().rstrip().endswith(
        "exec /usr/local/libexec/worklink-execd")
    dockerfile = (tmp_path / "Dockerfile").read_text()
    assert "RUN sh /opt/cultivate-docker/service.sh" in dockerfile
    assert 'ENTRYPOINT ["/init"]' in dockerfile
    assert "MIMIR_ENABLE_OPENCODE" in (tmp_path / "compose.yml").read_text()
    start = (tmp_path / "start.sh").read_text()
    assert 'os.environ.get("MIMIR_PLUGINS", "")' in start
    assert "MIMIR_CODING_ENABLED" not in start


@pytest.mark.parametrize("mode", ["root", "pypi"])
def test_build_bootstrap_selects_provider_only_for_invocation(tmp_path: Path, mode: str) -> None:
    dockerfile = ((ROOT / "Dockerfile").read_text() if mode == "root"
                  else render_dockerfile([], mode="pypi", plugins=(_candidate(),)))
    assert "ENV MIMIR_PLUGINS=cultivate" not in dockerfile
    assert 'MIMIR_PLUGINS=cultivate MIMIR_ENABLE_OPENCODE="$MIMIR_ENABLE_OPENCODE"' in dockerfile
    if mode == "pypi":
        assert dockerfile.index('pip install --no-cache-dir "mimir-cultivate==0.9.10"') < dockerfile.index(
            'MIMIR_PLUGINS=cultivate MIMIR_ENABLE_OPENCODE="$MIMIR_ENABLE_OPENCODE"')
    stub = tmp_path / "mimir"
    stub.write_text('#!/bin/sh\nprintf "%s:%s\\n" "$MIMIR_PLUGINS" "$*" >> "$BOOT_LOG"\n')
    stub.chmod(0o755)
    output = tmp_path / "bootstrap.log"
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "BOOT_LOG": str(output)}
    for enabled in ("0", "1"):
        subprocess.run(["sh", str(SCRIPTS / "bootstrap.sh")], check=True,
                       env={**env, "MIMIR_PLUGINS": "cultivate", "MIMIR_ENABLE_OPENCODE": enabled})
    assert output.read_text() == "cultivate:opencode-bootstrap --home /home/mimir\n"


def test_actual_selected_cli_bootstraps_and_missing_provider_fails(tmp_path: Path) -> None:
    env = {**os.environ, "MIMIR_PLUGINS": "cultivate"}
    result = subprocess.run([sys.executable, "-m", "mimir.cli", "opencode-bootstrap", "--home",
                             str(tmp_path / "home")], cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "home/.config/opencode/opencode.json").is_file()

    code = ("import importlib.metadata as metadata; metadata.entry_points = lambda **kw: (); "
            "from mimir.cli import main; main()")
    missing = subprocess.run([sys.executable, "-c", code, "opencode-bootstrap", "--home", str(tmp_path)],
                             cwd=ROOT, env=env, capture_output=True, text=True)
    assert missing.returncode != 0
    assert "MIMIR_PLUGINS: missing plugin(s): cultivate" in missing.stderr


@pytest.mark.parametrize("mode", ["root", "workspace", "pypi"])
def test_shared_service_script_provisions_each_layout(tmp_path: Path, monkeypatch, mode: str) -> None:
    if mode != "root":
        monkeypatch.setattr("mimir.scaffold_docker.select_plugins", lambda **kwargs: (_candidate(),))
        home = tmp_path / "home"
        home.mkdir()
        scaffold(home, mode=mode)
        assert "RUN sh /opt/cultivate-docker/service.sh" in (home / "Dockerfile").read_text()
        assert (home / ".mimir-scaffold/s6-rc.d/worklink-execd/run").is_file()
    else:
        assert "sh /opt/cultivate-docker/service.sh" in (ROOT / "Dockerfile").read_text()

    s6 = tmp_path / "s6-rc.d"
    source_layout = (ROOT / "deploy/s6-overlay/s6-rc.d" if mode == "root"
                     else home / ".mimir-scaffold/s6-rc.d")
    for name in ("mimir", "worklink-execd") + (("watchdog",) if mode == "root" else ()):
        run = s6 / name / "run"
        run.parent.mkdir(parents=True)
        run.write_bytes((source_layout / name / "run").read_bytes())
        run.chmod(0o644)
    libexec = tmp_path / "libexec"
    source = (SCRIPTS / "service.sh").read_text()
    source = source.replace("/etc/s6-overlay/s6-rc.d", str(s6)).replace(
        "/usr/local/libexec", str(libexec)).replace("install -d -o root -g root -m 0755", "mkdir -p")
    subprocess.run(["sh", "-c", source], check=True)
    for name in ("mimir", "worklink-execd") + (("watchdog",) if mode == "root" else ()):
        assert (s6 / name / "run").stat().st_mode & stat.S_IXUSR
    assert (libexec / "worklink-execd").stat().st_mode & stat.S_IXUSR
    assert "mimir_cultivate.worklink.worker_exec" in (libexec / "worklink-execd").read_text()
