"""Job ledger and review decisions for theorem.extraction.v1."""

from __future__ import annotations

import uuid
from datetime import datetime

from django.core.exceptions import ValidationError
from django.db import models

from apps.orchestration.models import Job as OrchestrationJob
from apps.tenancy.models import Tenant


class ExtractionJob(models.Model):
    class Operation(models.TextChoices):
        ATLAS = "atlas", "ATLAS"
        TYPED = "typed", "Typed"

    class SourceKind(models.TextChoices):
        ARTIFACT = "artifact", "Artifact"
        WEB_CORPUS = "web_corpus", "Web corpus"
        LIFE_EMAIL = "life_email", "Life email"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        PARTIAL = "partial", "Partial"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        CANCELED = "canceled", "Canceled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="extraction_jobs",
        db_column="tenant_id",
    )
    operation = models.CharField(max_length=16, choices=Operation.choices)
    contract_version = models.CharField(
        max_length=64,
        default="theorem.extraction.v1",
    )
    source_kind = models.CharField(max_length=32, choices=SourceKind.choices)
    source_ref = models.JSONField(default=dict)
    params = models.JSONField(default=dict)
    params_hash = models.CharField(max_length=64, db_index=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.QUEUED)
    shard_count = models.PositiveIntegerField(default=0)
    rows_total = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "control_extractionjob"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "operation", "params_hash", "source_ref"],
                name="control_extract_job_idempotent_uniq",
            )
        ]

    def __str__(self) -> str:
        return f"{self.operation} [{self.status}] {self.id}"


class ExtractionShard(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        CANCELED = "canceled", "Canceled"
        SUPERSEDED = "superseded", "Superseded"

    job = models.ForeignKey(
        ExtractionJob,
        on_delete=models.CASCADE,
        related_name="shards",
    )
    index = models.PositiveIntegerField()
    orchestration_job = models.OneToOneField(
        OrchestrationJob,
        on_delete=models.PROTECT,
        related_name="extraction_shard",
        null=True,
        blank=True,
    )
    input_artifact_key = models.CharField(max_length=512)
    input_digest = models.CharField(max_length=71)
    input_schema_json = models.TextField()
    input_rows = models.PositiveIntegerField()
    output_artifact_key = models.CharField(max_length=512, blank=True, default="")
    output_rows = models.PositiveIntegerField(null=True, blank=True)
    output_digest = models.CharField(max_length=71, blank=True, default="")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.QUEUED)
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "control_extractionshard"
        ordering = ["job_id", "index"]
        constraints = [
            models.UniqueConstraint(
                fields=["job", "index"],
                name="control_extract_shard_job_index_uniq",
            )
        ]

    def __str__(self) -> str:
        return f"{self.job_id}:{self.index} [{self.status}]"


class ExtractionReview(models.Model):
    class Decision(models.TextChoices):
        ACCEPT = "accept", "Accept"
        REJECT = "reject", "Reject"
        MERGE_INTO = "merge_into", "Merge into"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="extraction_reviews",
        db_column="tenant_id",
    )
    job = models.ForeignKey(
        ExtractionJob,
        on_delete=models.SET_NULL,
        related_name="reviews",
        null=True,
        blank=True,
    )
    candidate_digest = models.CharField(max_length=71, db_index=True)
    claim_id = models.CharField(max_length=512, null=True, blank=True)
    decision = models.CharField(max_length=16, choices=Decision.choices)
    merge_target_claim_id = models.CharField(max_length=512, null=True, blank=True)
    reason = models.TextField(blank=True, default="")
    reviewer = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "control_extractionreview"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "candidate_digest", "created_at"],
                name="control_extract_review_recency_uniq",
            )
        ]

    def clean(self) -> None:
        if self.decision == self.Decision.MERGE_INTO and not self.merge_target_claim_id:
            raise ValidationError(
                {"merge_target_claim_id": "merge_into requires a target claim id"}
            )
        if self.decision != self.Decision.MERGE_INTO and self.merge_target_claim_id:
            raise ValidationError(
                {"merge_target_claim_id": "only merge_into may carry a target claim id"}
            )

    def __str__(self) -> str:
        return f"{self.candidate_digest[:12]} [{self.decision}]"


def latest_reviews_since(
    tenant: Tenant,
    since: datetime,
) -> list[ExtractionReview]:
    """Return the newest decision per candidate after ``since`` portably."""
    latest: dict[str, ExtractionReview] = {}
    queryset = (
        ExtractionReview.objects.filter(tenant=tenant, created_at__gte=since)
        .select_related("job")
        .order_by("candidate_digest", "-created_at", "-id")
    )
    for review in queryset:
        latest.setdefault(review.candidate_digest, review)
    return sorted(latest.values(), key=lambda review: review.created_at)
