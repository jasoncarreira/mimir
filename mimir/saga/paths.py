"""Non-creating Saga path resolution shared by runtime and read-only guards."""
from __future__ import annotations

import logging
import os
from pathlib import Path
import tomllib


def resolve_saga_config(home: Path) -> tuple[Path | None, str]:
    """Choose this home's SAGA config without changing process state."""
    env_config = os.environ.get("SAGA_CONFIG")
    if env_config:
        return Path(env_config), "env"
    home_config = home / "saga.toml"
    if home_config.exists():
        return home_config, "home"
    return None, "defaults"


def resolve_saga_db_path(home: Path, *, db_path: str | Path | None = None) -> Path:
    """Resolve storage without creating directories or caching another home's config.

    Embedding initialization supplies its installed config's storage value;
    runtime/inspection callers read the selected home's file independently.
    Both use the same relative-path rule.
    """
    if db_path is None:
        from ._config_io import _DEFAULTS

        config_path, _ = resolve_saga_config(home)
        if config_path is not None and config_path.is_file():
            try:
                with config_path.open("rb") as stream:
                    config = tomllib.load(stream)
            except (OSError, ValueError) as exc:
                logging.getLogger("saga.config").warning(
                    "Failed to load %s: %s. Using defaults.", config_path, exc
                )
                config = {}
            db_path = config.get("storage", {}).get("db_path", "saga.db")
        else:
            db_path = _DEFAULTS["storage"]["db_path"]
    path = Path(db_path)
    return path if path.is_absolute() else home / ".mimir" / path
