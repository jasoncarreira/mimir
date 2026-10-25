"""Turn-scoped untrusted offload stores and one-time legacy-content purge."""

from __future__ import annotations

import logging
import os
import re
import stat
from contextlib import nullcontext
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING

from ._rmtree import rmtree_missing_ok

if TYPE_CHECKING:
    from .plugins.registry import PolicyRegistry

_STORES = ("large_tool_results", "conversation_history", "attachments/fetch-cache",
           ".mimir/conversation_history_labels")
_MARKER = ".mimir/turn-stores-v1"
_IGNORE_ENTRIES = ("logs/", "messages/", "large_tool_results/", "conversation_history/",
                   "attachments/fetch-cache/", "state/pollers/")
log = logging.getLogger(__name__)
_draft_owners: dict[Path, str] = {}
_draft_lock = RLock()
_store_lock = RLock()


def close_turn_stores(ctx: object) -> None:
    """Revoke allocation for retained tool-thread contexts before final cleanup."""
    with _store_lock:
        ctx.turn_stores_closed = True


def research_draft_write_guard(home: Path, raw_path: str):
    """Serialize draft write + ownership registration against another turn's wipe."""
    from ._context import get_current_turn
    from .access_control import _is_research_proposal_poller, get_trusted_service_from_auth_context

    turn = get_current_turn()
    service = get_trusted_service_from_auth_context(getattr(turn, "auth_context", None))
    if turn is None or not _is_research_proposal_poller(service):
        return nullcontext()
    root = home / "state" / "pollers" / service.canonical.removeprefix("poller:")
    target = home / raw_path.lstrip("/")
    try:
        relative = target.relative_to(root)
    except ValueError:
        return nullcontext()
    return _draft_lock if target.suffix == ".md" and (
        relative.name.endswith("draft.md") or "drafts" in relative.parts
    ) else nullcontext()


def record_research_draft(home: Path, raw_path: str) -> None:
    """Track successful model writes to ephemeral research draft markdown."""
    from ._context import get_current_turn
    from .access_control import _is_research_proposal_poller, get_trusted_service_from_auth_context

    ctx = get_current_turn()
    service = get_trusted_service_from_auth_context(getattr(ctx, "auth_context", None))
    if ctx is None or not _is_research_proposal_poller(service):
        return
    root = home / "state" / "pollers" / service.canonical.removeprefix("poller:")
    target = home / raw_path.lstrip("/")
    try:
        relative = target.relative_to(root)
        if (not target.is_file() or target.is_symlink() or target.suffix != ".md"
                or not (relative.name.endswith("draft.md") or "drafts" in relative.parts)
                or target.resolve(strict=True) != target):
            return
    except (ValueError, OSError, RuntimeError):
        return
    with _draft_lock:
        _draft_owners[target] = ctx.turn_id


def wipe_research_drafts(home: Path, turn_id: str) -> None:
    """Delete only draft files last written by this turn, never another turn's."""
    with _draft_lock:
        paths = [path for path, owner in _draft_owners.items() if owner == turn_id]
        for path in paths:
            try:
                parts = path.relative_to(home).parts
                fd = _open_directory(home, parts[:-1], create=False)
                try:
                    st = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
                    if stat.S_ISREG(st.st_mode):
                        os.unlink(parts[-1], dir_fd=fd)
                finally:
                    os.close(fd)
            except (OSError, ValueError):
                log.warning("research_draft_wipe_failed")
            finally:
                _draft_owners.pop(path, None)


def seed_home_ignore(home: Path) -> None:
    """Best-effort seed: an unsafe .ignore must not prevent server startup."""
    try:
        _seed_home_ignore(home)
    except OSError:
        log.warning("home_ignore_seed_failed")


def _seed_home_ignore(home: Path) -> None:
    fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        target = os.open(".ignore", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        try:
            with os.fdopen(target, "r+", encoding="utf-8") as stream:
                content = stream.read()
                missing = [line for line in _IGNORE_ENTRIES if line not in content.splitlines()]
                if missing:
                    stream.seek(0, os.SEEK_END)
                    stream.write(("\n" if content and not content.endswith("\n") else "")
                                 + "\n".join(missing) + "\n")
        finally:
            pass  # fdopen owns the descriptor
    finally:
        os.close(fd)


def _open_directory(home: Path, parts: tuple[str, ...], *, create: bool) -> int:
    """Open an owned directory chain; never traverse symlinked containers."""
    fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _remove_entry(fd: int, name: str) -> int:
    """Count regular files without following links, then remove the entry."""
    st = os.stat(name, dir_fd=fd, follow_symlinks=False)
    if not stat.S_ISDIR(st.st_mode):
        os.unlink(name, dir_fd=fd)
        return int(stat.S_ISREG(st.st_mode))
    count = 0
    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
    try:
        for entry in os.listdir(child):
            count += _count_files(child, entry)
    finally:
        os.close(child)
    rmtree_missing_ok(name, dir_fd=fd)
    return count


def _count_files(fd: int, name: str) -> int:
    st = os.stat(name, dir_fd=fd, follow_symlinks=False)
    if not stat.S_ISDIR(st.st_mode):
        return int(stat.S_ISREG(st.st_mode))
    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
    try:
        return sum(_count_files(child, item) for item in os.listdir(child))
    finally:
        os.close(child)


def turn_store(name: str) -> str | None:
    """Backend-virtual path for one live turn under an existing labeled root."""
    if name not in _STORES:
        raise ValueError("unknown turn store")
    from ._context import get_current_turn
    from .access_control import current_turn_scratch_root

    ctx = get_current_turn()
    home = os.environ.get("MIMIR_HOME", "")
    turn_id = getattr(ctx, "turn_id", None)
    if not home or current_turn_scratch_root() is None or not isinstance(turn_id, str):
        return None
    with _store_lock:
        if getattr(ctx, "turn_stores_closed", False):
            return None
        try:
            fd = _open_directory(Path(home).expanduser().resolve(), (*name.split("/"), turn_id), create=True)
            os.close(fd)
        except OSError:
            return None
        return f"/{name}/{turn_id}"


def wipe_turn_stores(home: Path, turn_id: str) -> None:
    """Remove only this turn's entries, including on failed/cancelled turns."""
    from .access_control import _valid_turn_scratch_id

    if not _valid_turn_scratch_id(turn_id):
        return
    for name in _STORES:
        try:
            fd = _open_directory(home, tuple(name.split("/")), create=False)
            try:
                _remove_entry(fd, turn_id)
            finally:
                os.close(fd)
        except FileNotFoundError:
            pass
        except OSError:
            log.warning("turn_store_wipe_failed store=%s", name)


def move_ci_logs_to_turn(home: Path, event: object, scratch: Path | None) -> tuple[Path, ...]:
    """Deliver bounded CI tails in the consuming turn's own scratch."""
    if (getattr(event, "service_principal", None) != "poller:github-ci-watch"
            or scratch is None):
        return ()
    source = home / "state" / "pollers" / "github-ci-watch" / "logs"
    content = getattr(event, "content", "")
    if not isinstance(content, str):
        return ()
    paths = re.findall(re.escape(str(source)) + r"/[0-9]+-[0-9]+\.log\b", content)
    if not paths:
        return ()
    moved = []
    try:
        source_fd = _open_directory(home, ("state", "pollers", "github-ci-watch", "logs"), create=False)
        target_fd = _open_directory(home, ("scratch", "turns", scratch.name, "ci-logs"), create=True)
    except OSError:
        return ()
    try:
        for raw in dict.fromkeys(paths):
            name = Path(raw).name
            try:
                if not stat.S_ISREG(os.stat(name, dir_fd=source_fd, follow_symlinks=False).st_mode):
                    continue
                os.rename(name, name, src_dir_fd=source_fd, dst_dir_fd=target_fd)
                destination = scratch / "ci-logs" / name
                content = content.replace(raw, str(destination))
                moved.append(destination)
            except OSError:
                log.warning("ci_log_delivery_failed")
    finally:
        os.close(source_fd)
        os.close(target_fd)
    event.content = content
    return tuple(moved)


def wipe_ci_logs(paths: tuple[Path, ...]) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            log.warning("ci_log_wipe_failed")


def _sweep_drafts(fd: int) -> None:
    """Walk research subdirectories without following links or deleting metadata."""
    for entry in os.listdir(fd):
        try:
            if entry == "drafts" or entry.endswith("draft.md"):
                _remove_entry(fd, entry)
            elif stat.S_ISDIR(os.stat(entry, dir_fd=fd, follow_symlinks=False).st_mode):
                child = os.open(entry, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    _sweep_drafts(child)
                finally:
                    os.close(child)
        except OSError:
            log.warning("research_draft_sweep_failed")


def _research_poller_names(home: Path, *, policy: PolicyRegistry | None = None) -> set[str]:
    """Only sweep pollers whose installed manifest grants research proposals."""
    from .access_control import _is_research_proposal_poller
    from .plugins.core import core_policy_registry
    from .pollers import discover_pollers

    if policy is None:
        policy = core_policy_registry()
    names: set[str] = set()
    for root in (home / "skills", home / ".mimir_builtin_skills"):
        try:
            configs = discover_pollers(root, state_root=home / "state/pollers", policy=policy)
            names.update(config.name for config in configs
                         if _is_research_proposal_poller(config.authority))
        except OSError:
            log.warning("research_poller_discovery_failed")
    return names


def sweep_turn_stores(home: Path, *, policy: PolicyRegistry | None = None) -> None:
    """On upgrade purge unattributed legacy bytes; on restart sweep crashed turns."""
    home = home.resolve()
    marker = home / _MARKER
    first_start = not marker.exists()
    counts: dict[str, int] = {}
    for name in _STORES:
        count = 0
        try:
            fd = _open_directory(home, tuple(name.split("/")), create=False)
        except FileNotFoundError:
            counts[name] = 0
            continue
        except OSError:
            log.warning("turn_store_sweep_failed store=%s", name)
            continue
        try:
            for entry in os.listdir(fd):
                count += _remove_entry(fd, entry)
        except OSError:
            log.warning("turn_store_sweep_failed store=%s", name)
        finally:
            os.close(fd)
        counts[name] = count
    # CI poller tails from a crashed delivery have no consumer on restart.
    try:
        fd = _open_directory(home, ("state", "pollers", "github-ci-watch", "logs"), create=False)
        try:
            for entry in os.listdir(fd):
                try:
                    _remove_entry(fd, entry)
                except OSError:
                    log.warning("ci_log_sweep_failed")
        finally:
            os.close(fd)
    except FileNotFoundError:
        pass
    except OSError:
        log.warning("ci_log_sweep_failed")
    # A process crash has no turn-finally. Restrict draft deletion to installed
    # research proposal pollers, never an arbitrary poller's drafts directory.
    for poller in sorted(_research_poller_names(home, policy=policy)):
        try:
            fd = _open_directory(home, ("state", "pollers", poller), create=False)
            try:
                _sweep_drafts(fd)
            finally:
                os.close(fd)
        except FileNotFoundError:
            pass
        except OSError:
            log.warning("research_draft_sweep_failed")
    if first_start:
        # Operator-owned Gmail audit records are intentionally not purged.
        try:
            fd = _open_directory(home, (".mimir",), create=True)
            try:
                marker_fd = os.open(marker.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                    0o600, dir_fd=fd)
                os.close(marker_fd)
            finally:
                os.close(fd)
        except FileExistsError:
            pass
        except OSError:
            log.warning("turn_store_marker_failed")
        log.info("turn_store_rollout_purge file_counts=%s", counts)
