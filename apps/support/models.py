"""Support notes for ops console."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models

from apps.tenancy.models import Tenant


class SupportNote(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="support_notes",
        db_column="tenant_id",
        null=True,
        blank=True,
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="support_notes",
    )
    subject = models.CharField(max_length=255)
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "control_supportnote"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.subject
