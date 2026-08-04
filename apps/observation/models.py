"""Feature flags and waitlist — ops surface, not product intelligence."""

from __future__ import annotations

import uuid

from django.db import models

from apps.tenancy.models import Tenant


class FeatureFlag(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.SlugField(max_length=128, unique=True)
    description = models.TextField(blank=True, default="")
    enabled_globally = models.BooleanField(default=False)
    tenant_overrides = models.JSONField(
        default=dict,
        blank=True,
        help_text="Map of tenant_id (str) -> bool",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "control_featureflag"
        ordering = ["key"]

    def __str__(self) -> str:
        return self.key

    def is_enabled_for(self, tenant: Tenant | None) -> bool:
        if tenant is not None:
            override = self.tenant_overrides.get(str(tenant.id))
            if override is not None:
                return bool(override)
        return self.enabled_globally


class Waitlist(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True)
    notes = models.TextField(blank=True, default="")
    invited_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "control_waitlist"
        ordering = ["-created_at"]
        verbose_name_plural = "waitlist"

    def __str__(self) -> str:
        return self.email
