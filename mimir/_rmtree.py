from __future__ import annotations

import os
import shutil
from typing import Callable


def _raise_unless_missing(
    _function: Callable[..., object],
    _path: str,
    exc_info: tuple[type[BaseException], BaseException, object],
) -> None:
    error = exc_info[1]
    if not isinstance(error, FileNotFoundError):
        raise error


def rmtree_missing_ok(path: str | os.PathLike[str] | bytes | os.PathLike[bytes]) -> None:
    """Remove a tree while tolerating entries removed concurrently."""
    shutil.rmtree(path, onerror=_raise_unless_missing)
