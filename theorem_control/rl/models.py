"""Tenant-owned training runs, trajectories, and deterministic grader verdicts."""

from __future__ import annotations

import uuid

from django.core.exceptions import ValidationError
from django.db import models

from apps.orchestration.artifacts import is_sha256_digest
from apps.tenancy.models import Tenant


def is_immutable_image_reference(value: str) -> bool:
    image, separator, digest = value.rpartition("@")
    return bool(image and separator and is_sha256_digest(digest))


class TrainingRun(models.Model):
    class Operation(models.TextChoices):
        EVAL = "eval", "Evaluation"
        PRIME_RL_TRAIN = "prime_rl.train", "prime-rl training"

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
        related_name="training_runs",
        db_column="tenant_id",
    )
    operation = models.CharField(max_length=32, choices=Operation.choices)
    operation_id = models.CharField(max_length=128)
    taskset_ref = models.CharField(max_length=512)
    config_digest = models.CharField(max_length=71)
    config_json = models.JSONField(default=dict)
    image_digest = models.CharField(max_length=512, blank=True, default="")
    pod_id = models.CharField(max_length=191, blank=True, default="")
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.QUEUED,
    )
    celery_task_id = models.CharField(max_length=255, blank=True, default="")
    artifact_keys = models.JSONField(default=list, blank=True)
    artifact_descriptors = models.JSONField(default=list, blank=True)
    error = models.TextField(blank=True, default="")
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "control_trainingrun"
        ordering = ["-created_at"]  # noqa: RUF012 - Django Meta contract
        constraints = [  # noqa: RUF012 - Django Meta contract
            models.UniqueConstraint(
                fields=["tenant", "operation_id"],
                name="control_trainingrun_tenant_operation_uniq",
            )
        ]
        indexes = [  # noqa: RUF012 - Django Meta contract
            models.Index(
                fields=["tenant", "status", "created_at"],
                name="control_trainrun_status_idx",
            )
        ]

    def clean(self) -> None:
        if not is_sha256_digest(self.config_digest):
            raise ValidationError({"config_digest": "must be a sha256 digest"})
        if (
            self.operation == self.Operation.PRIME_RL_TRAIN
            and not is_immutable_image_reference(self.image_digest)
        ):
            raise ValidationError(
                {"image_digest": "training requires an immutable image digest"}
            )

    def __str__(self) -> str:
        return f"{self.operation} [{self.status}] {self.operation_id}"


class Trajectory(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(
        TrainingRun,
        on_delete=models.CASCADE,
        related_name="trajectories",
    )
    task_key = models.CharField(max_length=255)
    trace_digest = models.CharField(max_length=71)
    reward = models.FloatField()
    metrics = models.JSONField(default=dict)
    arrow_shard_key = models.CharField(max_length=512)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "control_trajectory"
        ordering = ["run_id", "task_key", "id"]  # noqa: RUF012 - Django Meta contract
        constraints = [  # noqa: RUF012 - Django Meta contract
            models.UniqueConstraint(
                fields=["run", "trace_digest"],
                name="control_trajectory_run_trace_uniq",
            )
        ]
        indexes = [  # noqa: RUF012 - Django Meta contract
            models.Index(
                fields=["run", "task_key"],
                name="control_trajectory_task_idx",
            )
        ]

    def clean(self) -> None:
        if not is_sha256_digest(self.trace_digest):
            raise ValidationError({"trace_digest": "must be a sha256 digest"})


class GraderVerdict(models.Model):
    trajectory = models.OneToOneField(
        Trajectory,
        on_delete=models.CASCADE,
        related_name="grader_verdict",
        primary_key=True,
    )
    resolved = models.FloatField()
    metric_values = models.JSONField(default=dict)
    tripwire_flags = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "control_graderverdict"

    def clean(self) -> None:
        if not 0.0 <= self.resolved <= 1.0:
            raise ValidationError({"resolved": "must be between 0 and 1"})
        if self.tripwire_flags and self.resolved != 0.0:
            raise ValidationError(
                {"resolved": "a tripped grader verdict cannot be resolved"}
            )
