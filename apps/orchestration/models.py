"""Offload job records — control_job (D7)."""

from __future__ import annotations

import uuid

from django.db import models

from apps.tenancy.models import Tenant


class Job(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        CANCELED = "canceled", "Canceled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="jobs",
        db_column="tenant_id",
        null=True,
        blank=True,
    )
    operation = models.CharField(max_length=255)
    operation_id = models.CharField(
        max_length=128,
        db_index=True,
        help_text="Tenant-scoped idempotency key from cache_key_for_operation",
    )
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.QUEUED)
    celery_task_id = models.CharField(max_length=255, blank=True, default="")
    input_payload_digest = models.CharField(max_length=128, blank=True, default="")
    output_schema_json = models.TextField(blank=True, default="")
    output_artifact_key = models.CharField(max_length=512, blank=True, default="")
    output_payload_digest = models.CharField(max_length=128, blank=True, default="")
    output_rows = models.IntegerField(null=True, blank=True)
    logs = models.TextField(blank=True, default="")
    error = models.TextField(blank=True, default="")
    kwargs_json = models.JSONField(default=dict, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "control_job"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "operation_id"],
                name="control_job_tenant_operation_id_uniq",
            )
        ]

    def __str__(self) -> str:
        return f"{self.operation} [{self.status}] {self.operation_id[:12]}"
