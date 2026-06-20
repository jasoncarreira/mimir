"""Trusted first-party React dashboard extension registry.

This is intentionally not a marketplace or remote plugin loader.  The v1
contract is a typed manifest for dashboard tabs that ship with mimir, plus
optional hooks for first-party backend API namespaces.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
import re
from typing import Any

from aiohttp import web

_ID_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_API_NAMESPACE_RE = re.compile(r"^[a-z][a-z0-9-]*$")


@dataclass(frozen=True)
class DashboardExtensionManifest:
    """Manifest schema for a trusted first-party dashboard extension.

    Fields:
    - ``id``: stable kebab-case extension id.
    - ``route_path``: in-app React route path, relative to the ``/app`` shell.
    - ``label`` / ``icon``: navigation label and optional icon token.
    - ``nav_position``: lower values render earlier; ties sort by label/id.
    - ``enabled``: disabled manifests are hidden and do not register backend hooks.
    - ``bundle`` / ``css``: optional packaged static assets. Remote URLs are out
      of scope for v1; current first-party routes use the main Vite bundle.
    - ``api_namespace``: optional namespace such as ``"ops"`` that a backend hook
      may use to register ``/api/...`` routes.
    - ``trusted_first_party``: must remain true in v1. Arbitrary untrusted or
      remote plugins require a separate security/sandboxing project.
    """

    id: str
    route_path: str
    label: str
    icon: str | None = None
    nav_position: int = 100
    enabled: bool = True
    bundle: str | None = None
    css: tuple[str, ...] = ()
    api_namespace: str | None = None
    trusted_first_party: bool = True
    #: Minimum role required to SEE this nav entry in the React app (github
    #: #563). ``None`` = visible to any authenticated user; ``"admin"`` = the
    #: app hides it unless /whoami reports the admin role. UX only — the real
    #: boundary is the server-side ``/api/v1/admin/`` gate; this just keeps
    #: admin entries out of non-admin navigation.
    requires_role: str | None = None

    def validate(self) -> None:
        if not _ID_RE.fullmatch(self.id):
            raise ValueError(f"dashboard extension id must be kebab-case: {self.id!r}")
        if not self.route_path.startswith("/") or (
            self.route_path == "/app" or self.route_path.startswith("/app/")
        ):
            raise ValueError(
                f"dashboard extension route_path must be an in-app absolute path: {self.route_path!r}"
            )
        if not self.label.strip():
            raise ValueError(f"dashboard extension label is required: {self.id!r}")
        if self.api_namespace is not None and not _API_NAMESPACE_RE.fullmatch(self.api_namespace):
            raise ValueError(
                f"dashboard extension api_namespace must be kebab-case: {self.api_namespace!r}"
            )
        if not self.trusted_first_party:
            raise ValueError("dashboard extension v1 only accepts trusted first-party manifests")

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "route_path": self.route_path,
            "label": self.label,
            "icon": self.icon,
            "nav_position": self.nav_position,
            "enabled": self.enabled,
            "bundle": self.bundle,
            "css": list(self.css),
            "api_namespace": self.api_namespace,
            "trusted_first_party": self.trusted_first_party,
            "requires_role": self.requires_role,
        }


@dataclass(frozen=True)
class DashboardBackendRoute:
    method: str
    path: str
    handler: Callable[[web.Request], Any]


DashboardBackendHook = Callable[[], Sequence[DashboardBackendRoute]]


@dataclass
class DashboardExtensionRegistry:
    manifests: list[DashboardExtensionManifest] = field(default_factory=list)

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for manifest in self.manifests:
            manifest.validate()
            if manifest.id in seen:
                raise ValueError(f"duplicate dashboard extension id: {manifest.id!r}")
            seen.add(manifest.id)

    def enabled(self) -> list[DashboardExtensionManifest]:
        return sorted(
            (manifest for manifest in self.manifests if manifest.enabled),
            key=lambda manifest: (
                manifest.nav_position,
                manifest.label.lower(),
                manifest.id,
            ),
        )

    def navigation_payload(self) -> list[dict[str, Any]]:
        return [manifest.as_dict() for manifest in self.enabled()]


def first_party_dashboard_extensions(
    overrides: Iterable[DashboardExtensionManifest] | None = None,
) -> DashboardExtensionRegistry:
    """Return the bundled trusted dashboard extension registry."""

    manifests = list(
        overrides
        if overrides is not None
        else (
            DashboardExtensionManifest(
                id="chat",
                route_path="/chat",
                label="Chat",
                icon="message-circle",
                nav_position=10,
            ),
            DashboardExtensionManifest(
                id="turns",
                route_path="/turns",
                label="Turn Viewer",
                icon="list-tree",
                nav_position=20,
                api_namespace="turns",
            ),
            DashboardExtensionManifest(
                id="ops",
                route_path="/ops",
                label="Usage",
                icon="activity",
                nav_position=11,
                api_namespace="ops",
            ),
            DashboardExtensionManifest(
                id="chainlink-board",
                route_path="/chainlink",
                label="Tasks",
                icon="kanban",
                nav_position=35,
                api_namespace="chainlink-board",
            ),
            DashboardExtensionManifest(
                id="scheduler",
                route_path="/scheduler",
                label="Scheduler",
                icon="calendar-clock",
                nav_position=37,
                api_namespace="scheduler",
            ),
            DashboardExtensionManifest(
                id="saga",
                route_path="/saga",
                label="SAGA",
                icon="database",
                nav_position=40,
                api_namespace="saga",
            ),
            DashboardExtensionManifest(
                id="state-memory",
                route_path="/memory",
                label="State/Memory",
                icon="folder-tree",
                nav_position=50,
                api_namespace="memory",
            ),
            DashboardExtensionManifest(
                id="admin-config",
                route_path="/admin",
                label="Admin",
                icon="settings",
                nav_position=60,
                api_namespace="admin-config",
                requires_role="admin",
            ),
            DashboardExtensionManifest(
                id="admin-users",
                route_path="/admin/users",
                label="Users",
                icon="users",
                nav_position=61,
                api_namespace="admin-users",
                requires_role="admin",
            ),
        )
    )
    return DashboardExtensionRegistry(manifests)


def add_backend_namespace_routes(
    app: web.Application,
    *,
    registry: DashboardExtensionRegistry,
    hooks: dict[str, DashboardBackendHook],
    existing: set[tuple[str, str]] | None = None,
) -> None:
    """Register backend routes declared by enabled first-party manifests.

    A manifest's ``api_namespace`` is only a routing key into trusted Python
    hooks supplied by mimir. Unknown namespaces are ignored so a manifest can be
    frontend-only without creating partial API routes.
    """

    seen = existing if existing is not None else {
        (route.method, route.resource.canonical) for route in app.router.routes()
    }
    for manifest in registry.enabled():
        namespace = manifest.api_namespace
        if not namespace or namespace not in hooks:
            continue
        for route in hooks[namespace]():
            method = route.method.upper()
            if (method, route.path) in seen:
                continue
            if method == "GET":
                app.router.add_get(route.path, route.handler)
            elif method == "POST":
                app.router.add_post(route.path, route.handler)
            else:
                app.router.add_route(method, route.path, route.handler)
            seen.add((method, route.path))
