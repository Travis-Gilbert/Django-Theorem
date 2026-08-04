"""Org→tenant Valkey cache + membership invalidate (D4).

Key shapes (SPEC remaining closure):
- Org lookup (global): ``control:org:{org_id}:tenant_slug``
- Membership cache: ``t:{tenant_slug}:control:membership:{user_id}``
- Invalidate channel: ``control:membership:invalidate``

Redis is optional: when ``VALKEY_URL`` / ``REDIS_URL`` is unset/empty, or the
client cannot connect, an in-process dict backs the same API for tests.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)

MEMBERSHIP_INVALIDATE_CHANNEL = "control:membership:invalidate"
ORG_KEY_TTL_SECONDS = 300
MEMBERSHIP_KEY_TTL_SECONDS = 300

_memory_lock = threading.Lock()
_memory: dict[str, str] = {}


def _configured_url() -> str:
    url = getattr(settings, "VALKEY_URL", "") or ""
    if url:
        return url
    return getattr(settings, "REDIS_URL", "") or ""


def _org_key(org_id: str) -> str:
    return f"control:org:{org_id}:tenant_slug"


def _membership_key(tenant_slug: str, user_id: str) -> str:
    return f"t:{tenant_slug}:control:membership:{user_id}"


def _redis_client() -> Any | None:
    url = _configured_url()
    if not url:
        return None
    try:
        import redis

        client = redis.from_url(url)
        client.ping()
        return client
    except Exception as exc:  # noqa: BLE001 — optional Redis
        logger.debug("tenant_cache: redis unavailable (%s); using memory", exc)
        return None


def get_tenant_slug_for_org(org_id: str) -> str | None:
    """Return cached tenant slug for a WorkOS organization id, or None."""
    if not org_id:
        return None
    key = _org_key(org_id)
    client = _redis_client()
    if client is None:
        with _memory_lock:
            return _memory.get(key)
    try:
        value = client.get(key)
        if value is None:
            return None
        return value.decode("utf-8") if isinstance(value, bytes) else str(value)
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_tenant_slug_for_org failed: %s", exc)
        with _memory_lock:
            return _memory.get(key)


def set_tenant_slug_for_org(org_id: str, tenant_slug: str, *, ttl: int = ORG_KEY_TTL_SECONDS) -> None:
    """Warm the org→tenant_slug mapping (webhook organization bind)."""
    if not org_id or not tenant_slug:
        return
    key = _org_key(org_id)
    client = _redis_client()
    if client is None:
        with _memory_lock:
            _memory[key] = tenant_slug
        return
    try:
        client.setex(key, ttl, tenant_slug)
    except Exception as exc:  # noqa: BLE001
        logger.warning("set_tenant_slug_for_org failed: %s", exc)
        with _memory_lock:
            _memory[key] = tenant_slug


def invalidate_membership(tenant_id: str, user_id: str) -> None:
    """Drop membership cache entry and publish invalidate for Rust peers.

    ``tenant_id`` is the tenant *slug* (Valkey keyspace uses slug, not UUID).
    """
    if not tenant_id or not user_id:
        return
    key = _membership_key(tenant_id, user_id)
    payload = json.dumps(
        {
            "tenant_slug": tenant_id,
            "user_id": user_id,
            "action": "invalidate",
        }
    )
    client = _redis_client()
    if client is None:
        with _memory_lock:
            _memory.pop(key, None)
        logger.info(
            "StubMembershipInvalidate: deleted %s; publish deferred (no Valkey)",
            key,
        )
        return
    try:
        client.delete(key)
        client.publish(MEMBERSHIP_INVALIDATE_CHANNEL, payload)
        logger.info("invalidated membership %s on %s", key, MEMBERSHIP_INVALIDATE_CHANNEL)
    except Exception as exc:  # noqa: BLE001
        logger.warning("invalidate_membership failed for %s (%s)", key, exc)
        with _memory_lock:
            _memory.pop(key, None)


def clear_memory_cache() -> None:
    """Test helper — wipe the in-process fallback store."""
    with _memory_lock:
        _memory.clear()
