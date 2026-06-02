"""Mimir — memory-centric agent harness."""

from importlib import metadata as _metadata

try:
    # Single source of truth: the installed distribution's version (set from
    # pyproject.toml at build/install time). Reading it dynamically keeps
    # ``mimir.__version__`` from going stale every release — the bug behind a
    # false "update available" algedonic signal back when this was a hardcoded
    # literal (chainlink #345).
    __version__ = _metadata.version("mimir-agent")
except _metadata.PackageNotFoundError:  # source tree with no installed dist
    __version__ = "0.0.0+unknown"

del _metadata
