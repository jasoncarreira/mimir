"""Client-host path identities and lexical cwd policy shared with the ACP proxy.

This module is stdlib-only. Client paths must never be resolved against the
remote daemon filesystem; host-side scope validation resolves them separately.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

CLIENT_FILE_RESOURCE_NAMESPACE = "client-file"
_CLIENT_FILE_UNRESERVED = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)


def canonical_client_file_resource(path: object, cwd: object = None) -> str | None:
    import posixpath

    if not isinstance(path, str) or not path or "\x00" in path:
        return None
    if cwd is not None:
        if (
            not isinstance(cwd, str) or not cwd.startswith("/")
            or "\x00" in cwd
        ):
            return None
        try:
            cwd.encode("utf-8")
        except UnicodeEncodeError:
            return None
        # POSIX lexical confinement only; never resolve client-hosted symlinks.
        path = posixpath.normpath("/" + posixpath.join(cwd, path).lstrip("/"))
    try:
        encoded = path.encode("utf-8")
    except UnicodeEncodeError:
        return None
    identity = "".join(
        chr(value) if value in _CLIENT_FILE_UNRESERVED else f"%{value:02X}"
        for value in encoded
    )
    return f"{CLIENT_FILE_RESOURCE_NAMESPACE}:{identity}"


def client_file_resource_path(resource: object) -> str | None:
    """Decode a client resource once, accepting equivalent percent encodings."""
    prefix = f"{CLIENT_FILE_RESOURCE_NAMESPACE}:"
    if not isinstance(resource, str) or not resource.startswith(prefix):
        return None
    encoded_identity = resource[len(prefix):]
    if not encoded_identity:
        return None
    decoded = bytearray()
    index = 0
    while index < len(encoded_identity):
        value = encoded_identity[index]
        if ord(value) in _CLIENT_FILE_UNRESERVED:
            decoded.append(ord(value))
            index += 1
            continue
        if (
            value != "%"
            or index + 2 >= len(encoded_identity)
            or not re.fullmatch(r"[0-9A-Fa-f]{2}", encoded_identity[index + 1:index + 3])
        ):
            return None
        decoded.append(int(encoded_identity[index + 1:index + 3], 16))
        index += 3
    try:
        path = bytes(decoded).decode("utf-8")
    except UnicodeDecodeError:
        return None
    return path if canonical_client_file_resource(path) is not None else None


def client_file_resource_is_canonical(resource: object) -> bool:
    path = client_file_resource_path(resource)
    return path is not None and canonical_client_file_resource(path) == resource


@dataclass(frozen=True)
class ClientFileResourcePolicy:
    namespace: str
    grant: str

    @classmethod
    def for_cwd(cls, cwd: object) -> ClientFileResourcePolicy:
        resource = (
            canonical_client_file_resource(cwd, cwd="/")
            if isinstance(cwd, str) and cwd.startswith("/") else None
        )
        path = client_file_resource_path(resource)
        grant = canonical_client_file_resource(path.rstrip("/") + "/") if path else None
        return cls(CLIENT_FILE_RESOURCE_NAMESPACE, f"{grant}*" if grant else "")

    def allows(self, resource: object) -> bool:
        if self.namespace != CLIENT_FILE_RESOURCE_NAMESPACE or not self.grant.endswith("*"):
            return False
        boundary = client_file_resource_path(self.grant[:-1])
        path = client_file_resource_path(resource)
        if not boundary or not boundary.startswith("/") or not boundary.endswith("/"):
            return False
        if not path or not path.startswith("/"):
            return False
        root = client_file_resource_path(canonical_client_file_resource(boundary, cwd="/"))
        path = client_file_resource_path(canonical_client_file_resource(path, cwd="/"))
        return root is not None and path is not None and (
            path == root or path.startswith(root.rstrip("/") + "/")
        )


CLIENT_FILE_RESOURCE_POLICY = ClientFileResourcePolicy(
    namespace=CLIENT_FILE_RESOURCE_NAMESPACE,
    # Profile identity only. File grants must come from the bound session cwd.
    grant="",
)
