"""Valkey publish stub for key-revocation eviction (D5 / A6)."""

from __future__ import annotations

import json
import logging

from django.conf import settings

logger = logging.getLogger(__name__)

CHANNEL = "theorem:apikey:revoke"


def publish_key_revocation(key_id: str, key_prefix: str = "") -> None:
    """Publish a Valkey eviction message. Stub when redis is unreachable."""
    payload = json.dumps({"key_id": key_id, "key_prefix": key_prefix, "action": "revoke"})
    try:
        import redis

        client = redis.from_url(settings.VALKEY_URL)
        client.publish(CHANNEL, payload)
        logger.info("published key revocation for %s on %s", key_id, CHANNEL)
    except Exception as exc:  # noqa: BLE001 — stub must not break admin
        logger.warning(
            "StubValkeyPublish: failed to publish revocation for %s (%s); "
            "eviction deferred to cache TTL",
            key_id,
            exc,
        )
