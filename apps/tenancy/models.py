"""Tenant and project models — D3 read-model surface for Rust."""

from __future__ import annotations

import uuid

from django.db import models


class Tenant(models.Model):
    """control_tenant — Rust SELECT: id, slug, display_name, is_active."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    slug = models.SlugField(max_length=128, unique=True)
    display_name = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)
    workos_organization_id = models.CharField(
        max_length=128, blank=True, default="", db_index=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "control_tenant"
        ordering = ["slug"]

    def __str__(self) -> str:
        return f"{self.slug} ({self.display_name})"


class Project(models.Model):
    """control_project — Rust SELECT: tenant_id, slug, display_name."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="projects", db_column="tenant_id"
    )
    slug = models.SlugField(max_length=128)
    display_name = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "control_project"
        unique_together = [("tenant", "slug")]
        ordering = ["slug"]

    def __str__(self) -> str:
        return f"{self.tenant.slug}/{self.slug}"
