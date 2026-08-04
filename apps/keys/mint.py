"""Argon2id key mint helper for machine API keys (D5)."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from argon2 import PasswordHasher
from argon2.low_level import Type

from apps.keys.models import ApiKey
from apps.tenancy.models import Tenant

# Argon2id parameters — tune via env later if needed.
_HASHER = PasswordHasher(time_cost=2, memory_cost=65536, parallelism=2, type=Type.ID)

KEY_PREFIX = "thk_"


@dataclass(frozen=True)
class MintedKey:
    api_key: ApiKey
    plaintext: str  # shown once


def hash_api_key(plaintext: str) -> str:
    return _HASHER.hash(plaintext)


def verify_api_key(plaintext: str, key_hash: str) -> bool:
    try:
        return _HASHER.verify(key_hash, plaintext)
    except Exception:
        return False


def mint_api_key(
    tenant: Tenant,
    *,
    scopes: Sequence[str] | None = None,
    label: str = "",
    expires_at: datetime | None = None,
) -> MintedKey:
    """Generate a machine key, store Argon2id hash, return plaintext once."""
    raw = secrets.token_urlsafe(32)
    plaintext = f"{KEY_PREFIX}{raw}"
    prefix = plaintext[:12]
    api_key = ApiKey.objects.create(
        tenant=tenant,
        key_hash=hash_api_key(plaintext),
        key_prefix=prefix,
        label=label,
        scopes=list(scopes or []),
        expires_at=expires_at,
    )
    return MintedKey(api_key=api_key, plaintext=plaintext)
