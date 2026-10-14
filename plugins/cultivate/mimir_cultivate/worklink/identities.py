from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import grp
import pwd



@dataclass(frozen=True)
class WorklinkIdentities:
    mimir_uid: int
    worklink_uid: int
    worklink_gid: int


@lru_cache(maxsize=1)
def get_identities() -> WorklinkIdentities:
    try:
        mimir = pwd.getpwnam("mimir")
    except KeyError as exc:
        raise RuntimeError("required account 'mimir' is missing") from exc
    try:
        worklink = pwd.getpwnam("worklink")
    except KeyError as exc:
        raise RuntimeError("required account 'worklink' is missing") from exc
    try:
        worklink_group = grp.getgrnam("worklink")
    except KeyError as exc:
        raise RuntimeError("required group 'worklink' is missing") from exc
    return WorklinkIdentities(
        mimir_uid=mimir.pw_uid,
        worklink_uid=worklink.pw_uid,
        worklink_gid=worklink_group.gr_gid,
    )
