"""Tenant-scoped Valkey cache for canonical layout responses."""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from typing import Any

from django.conf import settings

from apps.layout.budget import (
    LAYOUT_CACHE_MIN_WRITE_BUDGET_SECONDS,
    LAYOUT_CACHE_SOCKET_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)
_memory_lock = threading.Lock()
_memory: OrderedDict[str, tuple[float, bytes]] = OrderedDict()


def cache_key(tenant_slug: str, digest_hex: str) -> str:
    return f"t:{tenant_slug}:layout:{digest_hex}"


def _remaining_seconds(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return deadline - time.monotonic()


def _redis_client(deadline: float | None = None) -> Any | None:
    url = getattr(settings, "VALKEY_URL", "") or getattr(settings, "REDIS_URL", "")
    if not url:
        return None
    remaining = _remaining_seconds(deadline)
    if remaining is not None and remaining <= 0:
        return None
    operation_timeout = LAYOUT_CACHE_SOCKET_TIMEOUT_SECONDS
    if remaining is not None:
        operation_timeout = min(operation_timeout, remaining)
    if operation_timeout <= 0:
        return None
    try:
        import redis

        client = redis.from_url(
            url,
            socket_connect_timeout=operation_timeout,
            socket_timeout=operation_timeout,
            retry_on_timeout=False,
        )
        client.ping()
        return client
    except Exception as exc:  # noqa: BLE001 - optional cache dependency
        logger.warning("layout cache Valkey unavailable; using process memory: %s", exc)
        return None


def _acquire_memory_lock(deadline: float | None) -> bool:
    remaining = _remaining_seconds(deadline)
    if remaining is None:
        _memory_lock.acquire()
        return True
    if remaining <= 0:
        return False
    return _memory_lock.acquire(timeout=remaining)


def get_cached_response(
    tenant_slug: str,
    digest_hex: str,
    *,
    deadline: float | None = None,
) -> bytes | None:
    key = cache_key(tenant_slug, digest_hex)
    client = _redis_client(deadline)
    if client is not None:
        try:
            value = client.get(key)
            return bytes(value) if value is not None else None
        except Exception as exc:  # noqa: BLE001 - cache failure must not fail layout
            logger.warning("layout cache read failed; using process memory: %s", exc)
    now = time.monotonic()
    if not _acquire_memory_lock(deadline):
        return None
    try:
        entry = _memory.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at <= now:
            _memory.pop(key, None)
            return None
        _memory.move_to_end(key)
        return value
    finally:
        _memory_lock.release()


def set_cached_response(
    tenant_slug: str,
    digest_hex: str,
    value: bytes,
    *,
    deadline: float | None = None,
) -> None:
    remaining = _remaining_seconds(deadline)
    if remaining is not None and remaining < LAYOUT_CACHE_MIN_WRITE_BUDGET_SECONDS:
        return
    key = cache_key(tenant_slug, digest_hex)
    ttl = int(getattr(settings, "LAYOUT_CACHE_TTL_SECONDS", 86_400))
    client = _redis_client(deadline)
    if client is not None:
        try:
            client.set(key, value, ex=ttl)
            return
        except Exception as exc:  # noqa: BLE001 - cache failure must not fail layout
            logger.warning("layout cache write failed; using process memory: %s", exc)
    if not _acquire_memory_lock(deadline):
        return
    try:
        _memory.pop(key, None)
        _memory[key] = (time.monotonic() + ttl, value)
        max_entries = max(
            1, int(getattr(settings, "LAYOUT_MEMORY_CACHE_MAX_ENTRIES", 1_024))
        )
        while len(_memory) > max_entries:
            _memory.popitem(last=False)
    finally:
        _memory_lock.release()


def clear_memory_cache() -> None:
    with _memory_lock:
        _memory.clear()
