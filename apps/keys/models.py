"""Machine API keys — control_apikey (D3 / D5)."""

from __future__ import annotations

import uuid

from django.db import models
from django.utils import timezone

from apps.tenancy.models import Tenant


class ApiKey(models.Model):
    """control_apikey — Rust SELECT: id, tenant_id, key_hash, scopes, revoked_at, expires_at."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="api_keys", db_column="tenant_id"
    )
    key_hash = models.CharField(max_length=512)
    key_prefix = models.CharField(max_length=16, blank=True, default="", db_index=True)
    label = models.CharField(max_length=128, blank=True, default="")
    scopes = models.JSONField(default=list)
    revoked_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "control_apikey"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.key_prefix}… ({self.tenant.slug})"

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= timezone.now()
