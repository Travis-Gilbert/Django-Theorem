"""Plans, subscriptions, and usage — D3 / D6."""

from __future__ import annotations

import uuid

from django.db import models

from apps.tenancy.models import Tenant


class Plan(models.Model):
    """control_plan — Rust SELECT: code, limits (JSON)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.SlugField(max_length=64, unique=True)
    display_name = models.CharField(max_length=128, blank=True, default="")
    limits = models.JSONField(
        default=dict,
        help_text="items, storage_bytes, monthly_harness_turns, embedding_tokens, concurrent_jobs",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "control_plan"
        ordering = ["code"]

    def __str__(self) -> str:
        return self.code


class Subscription(models.Model):
    """control_subscription — Rust SELECT: tenant_id, plan_code, status."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        PAST_DUE = "past_due", "Past due"
        CANCELED = "canceled", "Canceled"
        TRIALING = "trialing", "Trialing"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="subscriptions",
        db_column="tenant_id",
    )
    plan = models.ForeignKey(
        Plan,
        on_delete=models.PROTECT,
        related_name="subscriptions",
        to_field="code",
        db_column="plan_code",
    )
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.ACTIVE)
    stripe_subscription_id = models.CharField(max_length=128, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "control_subscription"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.tenant.slug}:{self.plan.code} ({self.status})"


class Usage(models.Model):
    """Per-tenant per-period usage counters — Django accumulates; Rust reports."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="usage_rows", db_column="tenant_id"
    )
    period_start = models.DateField()
    period_end = models.DateField()
    counters = models.JSONField(
        default=dict,
        help_text="harness_turns, embedding_tokens, storage_bytes, concurrent_jobs, ...",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "control_usage"
        unique_together = [("tenant", "period_start", "period_end")]

    def __str__(self) -> str:
        return f"{self.tenant.slug} {self.period_start}..{self.period_end}"
