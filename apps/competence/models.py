"""Inspectable fit/refit jobs for the theorem.competence.v1 boundary."""

from __future__ import annotations

import uuid

from django.db import models

from apps.tenancy.models import Project, Tenant


class CompetenceJob(models.Model):
    class Operation(models.TextChoices):
        FIT = "fit", "Fit"
        REFIT = "refit", "Refit"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        REFUSED = "refused", "Refused"
        CANCELED = "canceled", "Canceled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="competence_jobs",
        db_column="tenant_id",
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="competence_jobs",
        db_column="project_id",
    )
    operation = models.CharField(max_length=16, choices=Operation.choices)
    operation_id = models.CharField(max_length=128, db_index=True)
    request_digest = models.CharField(max_length=71)
    request_json = models.JSONField()
    status = models.CharField(
        max_length=32, choices=Status.choices, default=Status.QUEUED
    )
    scorer_json = models.JSONField(default=dict, blank=True)
    refusal_json = models.JSONField(default=dict, blank=True)
    artifact_keys_json = models.JSONField(default=dict, blank=True)
    cleanup_request_digest = models.CharField(max_length=71, blank=True, default="")
    cleanup_receipt_json = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True, default="")
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "control_competence_job"
        ordering = ["-created_at"]  # noqa: RUF012 - Django Meta contract
        constraints = [  # noqa: RUF012 - Django Meta contract
            models.UniqueConstraint(
                fields=["tenant", "operation_id"],
                name="control_competence_job_tenant_operation_uniq",
            )
        ]

    def __str__(self) -> str:
        return f"{self.operation} [{self.status}] {self.operation_id[:20]}"
