"""A core-only deployment has no Worklink identity or Worklink paths.

The ``worklink`` uid/group, ``/var/lib/mimir-worklink``, ``/opt/mimir-worklink``,
the executor socket and ``.worklink`` checkout roots belong to the Cultivate
plugin. Core setup, boot and scaffolding must neither create them nor look the
identity up unless Cultivate is selected.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from mimir.scaffold_docker import scaffold

_WORKLINK_MARKERS = (
    "worklink", "mimir-worklink", "worklink-execd", "users.sh", "executor.sh", "1002",
)


def _scaffold_text(home: Path) -> str:
    texts = []
    for path in sorted(home.rglob("*")):
        if path.is_file() and path.name != ".gitignore":
            texts.append(f"== {path.relative_to(home)}\n{path.read_text(errors='replace')}")
    return "\n".join(texts)


@pytest.mark.parametrize("mode", ["pypi", "workspace"])
def test_core_only_scaffold_sets_up_no_worklink_identity_or_paths(tmp_path, monkeypatch, mode):
    monkeypatch.setattr("mimir.scaffold_docker.select_plugins", lambda **kwargs: ())
    scaffold(tmp_path, mode=mode)
    text = _scaffold_text(tmp_path).lower()
    assert not (tmp_path / ".mimir-scaffold" / "cultivate-docker").exists()
    found = [marker for marker in _WORKLINK_MARKERS if marker in text]
    assert not found, found


@pytest.mark.parametrize("mode", ["pypi", "workspace"])
def test_cultivate_scaffold_does_set_up_the_worklink_identity(tmp_path, monkeypatch, mode):
    pytest.importorskip("mimir_cultivate")
    monkeypatch.setenv("MIMIR_PLUGINS", "cultivate")
    scaffold(tmp_path, mode=mode)
    users = (tmp_path / ".mimir-scaffold/cultivate-docker/users.sh").read_text()
    assert "useradd --no-create-home --uid 1002 --gid worklink" in users
    assert "/var/lib/mimir-worklink" in users
    dockerfile = (tmp_path / "Dockerfile").read_text()
    assert "RUN sh /opt/cultivate-docker/users.sh" in dockerfile
    assert "sh /opt/cultivate-docker/executor.sh" in dockerfile
    assert (tmp_path / ".mimir-scaffold/s6-rc.d/worklink-execd/run").is_file()


_BOOT = textwrap.dedent("""
    import grp
    import os
    import pwd
    import sys
    from pathlib import Path

    lookups = []

    def guard(module, name):
        real = getattr(module, name)

        def wrapped(key, *args, **kwargs):
            if "worklink" in str(key):
                lookups.append(f"{module.__name__}.{name}({key!r})")
            return real(key, *args, **kwargs)
        setattr(module, name, wrapped)

    guard(pwd, "getpwnam")
    guard(grp, "getgrnam")

    home = Path(sys.argv[1])
    watched = [Path(p) for p in sys.argv[2:]]
    before = {p: p.exists() for p in watched}

    from mimir.cli import main as cli_main
    try:
        cli_main(["setup", "--home", str(home)])
    except SystemExit as exc:
        assert exc.code in (0, None), exc.code
    os.environ["MIMIR_HOME"] = str(home)
    from mimir.config import Config
    from mimir import server
    config = Config.from_env()
    assert not config.plugin_manager.plugins
    app = server.build_app(config)
    assert app is not None

    created = [str(p) for p in watched if p.exists() and not before[p]]
    # Seeded reference docs may mention Worklink; no Worklink directory may exist.
    in_home = [str(p) for p in home.rglob("*") if p.is_dir() and "worklink" in p.name.lower()]
    assert not lookups, lookups
    assert not created, created
    assert not in_home, in_home
    assert not any(n == "mimir_cultivate" or n.startswith("mimir_cultivate.") for n in sys.modules)
    print("core boot ok")
""")


def test_core_only_setup_and_boot_touch_no_worklink_identity_or_paths(tmp_path):
    home = tmp_path / "home"
    watched = (
        "/var/lib/mimir-worklink", "/opt/mimir-worklink", "/run/mimir-worklink",
        "/workspace/.worklink", str(tmp_path / ".worklink"),
    )
    env = {key: value for key, value in os.environ.items() if not key.startswith("MIMIR_")}
    env.update({"MIMIR_PLUGINS": "", "HOME": str(tmp_path), "MIMIR_CODING_ENABLED": "true",
                "MIMIR_CLAUDE_OAUTH_CREDENTIALS": ""})
    result = subprocess.run(
        [sys.executable, "-c", _BOOT, str(home), *watched],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    assert "core boot ok" in result.stdout
