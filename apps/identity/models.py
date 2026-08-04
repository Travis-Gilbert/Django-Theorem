"""WorkOS shadow rows — control_user / control_membership."""

from __future__ import annotations

import uuid

from django.db import models

from apps.tenancy.models import Tenant


class User(models.Model):
    """Shadow of a WorkOS user. Identity is theirs; authorization is ours."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workos_user_id = models.CharField(max_length=128, unique=True)
    email = models.EmailField(blank=True, default="")
    display_name = models.CharField(max_length=255, blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "control_user"
        ordering = ["email"]

    def __str__(self) -> str:
        return self.email or self.workos_user_id


class Membership(models.Model):
    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        ADMIN = "admin", "Admin"
        MEMBER = "member", "Member"
        VIEWER = "viewer", "Viewer"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="memberships", db_column="tenant_id"
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="memberships", db_column="user_id"
    )
    role = models.CharField(max_length=32, choices=Role.choices, default=Role.MEMBER)
    workos_membership_id = models.CharField(max_length=128, blank=True, default="", db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "control_membership"
        unique_together = [("tenant", "user")]

    def __str__(self) -> str:
        return f"{self.user} @ {self.tenant.slug} ({self.role})"
