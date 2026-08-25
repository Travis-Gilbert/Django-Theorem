"""Tenant-scoped Valkey cache for canonical layout responses."""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)
_memory_lock = threading.Lock()
_memory: OrderedDict[str, tuple[float, bytes]] = OrderedDict()


def cache_key(tenant_slug: str, digest_hex: str) -> str:
    return f"t:{tenant_slug}:layout:{digest_hex}"


def _redis_client() -> Any | None:
    url = getattr(settings, "VALKEY_URL", "") or getattr(settings, "REDIS_URL", "")
    if not url:
        return None
    try:
        import redis

        client = redis.from_url(url)
        client.ping()
        return client
    except Exception as exc:  # noqa: BLE001 - optional cache dependency
        logger.warning("layout cache Valkey unavailable; using process memory: %s", exc)
        return None


def get_cached_response(tenant_slug: str, digest_hex: str) -> bytes | None:
    key = cache_key(tenant_slug, digest_hex)
    client = _redis_client()
    if client is not None:
        try:
            value = client.get(key)
            return bytes(value) if value is not None else None
        except Exception as exc:  # noqa: BLE001 - cache failure must not fail layout
            logger.warning("layout cache read failed; using process memory: %s", exc)
    now = time.monotonic()
    with _memory_lock:
        entry = _memory.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at <= now:
            _memory.pop(key, None)
            return None
        _memory.move_to_end(key)
        return value


def set_cached_response(tenant_slug: str, digest_hex: str, value: bytes) -> None:
    key = cache_key(tenant_slug, digest_hex)
    ttl = int(getattr(settings, "LAYOUT_CACHE_TTL_SECONDS", 86_400))
    client = _redis_client()
    if client is not None:
        try:
            client.setex(key, ttl, value)
            return
        except Exception as exc:  # noqa: BLE001 - cache failure must not fail layout
            logger.warning("layout cache write failed; using process memory: %s", exc)
    with _memory_lock:
        _memory.pop(key, None)
        _memory[key] = (time.monotonic() + ttl, value)
        max_entries = max(
            1, int(getattr(settings, "LAYOUT_MEMORY_CACHE_MAX_ENTRIES", 1_024))
        )
        while len(_memory) > max_entries:
            _memory.popitem(last=False)


def clear_memory_cache() -> None:
    with _memory_lock:
        _memory.clear()
