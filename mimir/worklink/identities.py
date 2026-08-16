from __future__ import annotations

from dataclasses import dataclass
import grp
import pwd


@dataclass(frozen=True)
class WorklinkIdentities:
    mimir_uid: int
    worklink_uid: int
    worklink_gid: int


def _resolve_identities() -> WorklinkIdentities:
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


IDENTITIES = _resolve_identities()
MIMIR_UID = IDENTITIES.mimir_uid
WORKLINK_UID = IDENTITIES.worklink_uid
WORKLINK_GID = IDENTITIES.worklink_gid
