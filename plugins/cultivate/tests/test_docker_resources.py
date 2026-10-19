from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from mimir.plugin_dockerfile import cultivate_scripts, has_trusted_cultivate, services
from mimir.scaffold_docker import render_dockerfile, scaffold


ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "plugins/cultivate/mimir_cultivate/docker"


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
    assert "MIMIR_ENABLE_OPENCODE" in (tmp_path / "start.sh").read_text()
