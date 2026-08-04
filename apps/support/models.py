"""Support notes + short-lived impersonation grants (D11)."""

from __future__ import annotations

import secrets
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.tenancy.models import Tenant

IMPERSONATION_MAX_TTL = timedelta(minutes=30)


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


class ImpersonationGrant(models.Model):
    """Short-lived scoped support token. Plaintext shown once in admin; store hash only."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    token_id = models.CharField(max_length=64, unique=True, db_index=True)
    token_hash = models.CharField(max_length=128)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="impersonation_grants",
        db_column="tenant_id",
    )
    subject_user_id = models.CharField(
        max_length=128,
        help_text="WorkOS user id being impersonated",
    )
    scopes = models.JSONField(default=list)
    expires_at = models.DateTimeField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="impersonation_grants_created",
    )
    audit_note = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "control_impersonationgrant"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.token_id} → {self.subject_user_id} ({self.tenant.slug})"

    @classmethod
    def mint(
        cls,
        *,
        tenant: Tenant,
        subject_user_id: str,
        created_by,
        scopes: list[str] | None = None,
        ttl: timedelta | None = None,
    ) -> tuple["ImpersonationGrant", str]:
        """Create a grant and return (row, plaintext_token). Plaintext is not stored."""
        ttl = ttl or IMPERSONATION_MAX_TTL
        if ttl > IMPERSONATION_MAX_TTL:
            ttl = IMPERSONATION_MAX_TTL
        token_id = f"imp_{secrets.token_urlsafe(12)}"
        plaintext = f"{token_id}.{secrets.token_urlsafe(24)}"
        import hashlib

        token_hash = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
        grant = cls.objects.create(
            token_id=token_id,
            token_hash=token_hash,
            tenant=tenant,
            subject_user_id=subject_user_id,
            scopes=scopes or ["support:impersonate"],
            expires_at=timezone.now() + ttl,
            created_by=created_by if getattr(created_by, "is_authenticated", False) else None,
            audit_note=(
                f"impersonate {subject_user_id} tenant={tenant.slug} "
                f"by={created_by} at={timezone.now().isoformat()}"
            ),
        )
        return grant, plaintext
