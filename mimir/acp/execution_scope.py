from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from mimir.client_file_resources import canonical_client_file_resource, client_file_resource_path

MAX_SCOPE_REQUESTS = 8
UNCONFINED_WARNING = "UNCONFINED: hands_shell/hands_python run with the local proxy user's filesystem permissions. The cwd and path-scope grants do NOT protect files. Operator approval applies only to this session."
SCOPE_WARNING = "This changes filesystem access for this session. The Python kernel will restart and all variables/imports will be lost."


def canonical_scope_path(value: str, cwd: Path) -> Path:
    if not isinstance(value, str) or not value or len(value) > 4096 or any(c in value for c in "*?[]"):
        raise ValueError("Request one existing file or directory, not a glob")
    resource = canonical_client_file_resource(value, cwd=str(cwd))
    lexical = client_file_resource_path(resource)
    if lexical is None:
        raise ValueError("Invalid scope path")
    try:
        path = Path(lexical).resolve(strict=True)
        if path == Path(path.anchor) or not (path.is_file() or path.is_dir()):
            raise ValueError("Scope must be a non-root file or directory")
    except (OSError, RuntimeError) as exc:
        raise ValueError("Scope must be an existing file or directory") from exc
    return path


def contains(root: Path, path: Path) -> bool:
    return root == path or (root.is_dir() and path.is_relative_to(root))


@dataclass(slots=True)
class ExecutionScope:
    cwd: Path
    approved: set[Path] = field(default_factory=set)
    denied: set[Path] = field(default_factory=set)
    execution_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending: bool = False
    attempts: int = 0
    generation: int = 0
    closed: bool = False
    unconfined_approved: bool = False
    risk_requested: bool = False
    risk_pending: bool = False

    def paths(self) -> list[str]:
        return sorted(str(p) for p in {self.cwd, *self.approved})

    def allows(self, path: Path) -> bool:
        return contains(self.cwd, path) or path in self.approved

    def rejected(self, path: Path) -> bool:
        return any(contains(p, path) or contains(path, p) for p in self.denied)

    def invalidate(self, *, close: bool = False) -> None:
        self.generation += 1
        self.closed |= close
