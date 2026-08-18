"""Source labels for protected prompt content."""

from __future__ import annotations

from .models import AuthContext, Integrity, IntegrityEffect, SourceLabel


def prompt_source_label(
    auth_context: AuthContext,
    *,
    domain: str,
    resource: str,
    principal: str | None,
    bridge_instance: str | None,
    authorized_principals: frozenset[str],
    channel_id: str | None = None,
    source_kind: str = "protected_prompt",
    self_authored: bool,
) -> SourceLabel:
    """Create a private, informational prompt source with explicit provenance."""
    target_channel = channel_id
    if self_authored and not target_channel:
        target_channel = auth_context.resource_id or auth_context.channel_id
    return SourceLabel(
        principal=principal,
        domain=domain,
        resource_id=target_channel or resource,
        bridge_instance=bridge_instance,
        sensitivity="private",
        authorized_principals=authorized_principals,
        source_kind=source_kind,
        integrity=Integrity.TRUSTED if self_authored else Integrity.UNTRUSTED,
        integrity_effect=IntegrityEffect.INFORMATIONAL,
    )
