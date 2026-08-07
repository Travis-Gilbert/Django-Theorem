"""Valkey publish for key-revocation eviction (D5 / A6).

Channel shapes follow Theorem `theorem-valkey-client` doctrine
(SPEC-THEOREM-VALKEY-DOCTRINE-1.0 / KEYSPACE.md):

- Tenant: ``t:{tenant_slug}:control_apikey:revoke``
- Global fan-out: ``t:_global:control_apikey:revoke``

Payload is the bare ``key_id`` string (Rust subscribers expect key_id, not JSON).
"""

from __future__ import annotations

import logging

from django.conf import settings

logger = logging.getLogger(__name__)

GLOBAL_TENANT = "_global"


def apikey_revoke_channel(tenant_slug: str) -> str:
    """``t:{tenant}:control_apikey:revoke`` — matches Rust ``apikey_revoke_channel``."""
    slug = (tenant_slug or "").strip() or GLOBAL_TENANT
    return f"t:{slug}:control_apikey:revoke"


def apikey_revoke_channel_global() -> str:
    """``t:_global:control_apikey:revoke`` — matches Rust ``apikey_revoke_channel_global``."""
    return apikey_revoke_channel(GLOBAL_TENANT)


def publish_key_revocation(
    key_id: str,
    key_prefix: str = "",
    *,
    tenant_slug: str = "",
) -> None:
    """Publish a Valkey eviction message. Stub when redis is unreachable.

    Prefer the tenant-scoped channel when ``tenant_slug`` is known. When it is
    empty, publish on the global fan-out channel so peers can still evict.
    """
    del key_prefix  # retained for admin call-site compatibility; not in payload
    channel = (
        apikey_revoke_channel(tenant_slug)
        if tenant_slug.strip()
        else apikey_revoke_channel_global()
    )
    payload = str(key_id)
    try:
        import redis

        client = redis.from_url(settings.VALKEY_URL)
        client.publish(channel, payload)
        logger.info("published key revocation for %s on %s", key_id, channel)
    except Exception as exc:  # noqa: BLE001 — stub must not break admin
        logger.warning(
            "StubValkeyPublish: failed to publish revocation for %s on %s (%s); "
            "eviction deferred to cache TTL",
            key_id,
            channel,
            exc,
        )
