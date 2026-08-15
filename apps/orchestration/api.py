"""Internal offload API — D7 / D6 hard quota."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from ninja import Field, Router, Schema
from ninja.errors import HttpError

from apps.billing.models import Subscription
from apps.keys.auth import (
    OFFLOAD_CANCEL_SCOPE,
    OFFLOAD_INVOKE_SCOPE,
    OFFLOAD_READ_SCOPE,
    require_machine_key,
)
from apps.orchestration.artifacts import (
    ArtifactConfigurationError,
    ArtifactStore,
    ArtifactStorageError,
    ArtifactValidationError,
    is_sha256_digest,
)
from apps.orchestration.models import Job
from apps.orchestration.tasks import dispatch_offload, cancel_job_task
from apps.tenancy.models import Tenant

router = Router(tags=["offload"])

REGISTERED_OPERATIONS = frozenset(
    {
        "data_science.tabfm.label_aggregate",
        "data_science.gnn.denoise",
        "data_science.gnn.embed",
        "data_science.community.assign",
        # R-backed statistical ops land on queue offload.r
        "data_science.r.survey_weight",
        "data_science.r.mixed_model",
        "data_science.r.survival",
    }
)

ACTIVE_JOB_STATUSES = (Job.Status.QUEUED, Job.Status.RUNNING)


class ArrowBatchDescriptor(Schema):
    # Avoid shadowing pydantic/ninja Schema.schema_json; wire name stays schema_json.
    arrow_schema_json: str = Field(default="", alias="schema_json")
    rows: int | None = None
    payload_digest: str = ""
    artifact_key: str = ""
    download_url: str = ""

    model_config = {"populate_by_name": True}


class InvokeRequest(Schema):
    operation: str
    operation_id: str
    input: ArrowBatchDescriptor
    input_entity_ids: list[str] = []
    params: dict[str, Any] = {}


class InvokeResponse(Schema):
    job_id: UUID
    operation_id: str
    status: str
    reused: bool = False


class ArtifactUploadResponse(Schema):
    artifact_key: str
    upload_url: str
    expires_in_seconds: int


class JobStatusResponse(Schema):
    job_id: UUID
    operation: str
    operation_id: str
    status: str
    output: ArrowBatchDescriptor | None = None
    error: str = ""
    logs: str = ""


def _enforce_concurrent_job_quota(tenant_id: UUID) -> None:
    """Hard refuse at dispatch when queued|running jobs meet plan.limits.concurrent_jobs (D6/A7)."""
    sub = (
        Subscription.objects.select_related("plan")
        .filter(tenant_id=tenant_id, status=Subscription.Status.ACTIVE)
        .order_by("-created_at")
        .first()
    )
    if sub is None:
        return
    limits = sub.plan.limits or {}
    raw = limits.get("concurrent_jobs")
    if raw is None:
        return
    try:
        limit = int(raw)
    except (TypeError, ValueError):
        return
    if limit < 0:
        return
    active = Job.objects.filter(tenant_id=tenant_id, status__in=ACTIVE_JOB_STATUSES).count()
    if active >= limit:
        raise HttpError(
            429,
            f"concurrent_jobs limit reached ({active}/{limit})",
        )


def _artifact_store_or_503() -> ArtifactStore:
    try:
        return ArtifactStore.from_settings()
    except ArtifactConfigurationError as exc:
        raise HttpError(503, f"artifact storage unavailable: {exc}") from exc


@router.post("/artifact-upload", response=ArtifactUploadResponse)
def begin_artifact_upload(request):
    """Mint a one-object upload capability inside the caller's tenant prefix."""
    principal = require_machine_key(request, scope=OFFLOAD_INVOKE_SCOPE)
    try:
        store = _artifact_store_or_503()
        artifact_key = store.allocate_input_key(principal.tenant.id)
        return ArtifactUploadResponse(
            artifact_key=artifact_key,
            upload_url=store.presign_put(principal.tenant.id, artifact_key),
            expires_in_seconds=store.presign_seconds,
        )
    except ArtifactStorageError as exc:
        raise HttpError(503, "artifact storage unavailable") from exc


@router.post("/invoke", response=InvokeResponse)
def invoke_offload(request, body: InvokeRequest):
    principal = require_machine_key(request, scope=OFFLOAD_INVOKE_SCOPE)
    if body.operation not in REGISTERED_OPERATIONS:
        raise HttpError(400, f"unknown operation: {body.operation}")
    requires_artifact = settings.OFFLOAD_EXECUTION_MODE == "runpod" or (
        body.operation.startswith("data_science.r.") and settings.R_OFFLOAD_EXECUTION_MODE == "rpy2"
    )
    if requires_artifact:
        store = _artifact_store_or_503()
        try:
            store.validate_key(principal.tenant.id, body.input.artifact_key)
            if not is_sha256_digest(body.input.payload_digest):
                raise ArtifactValidationError("input.payload_digest must be a sha256 digest")
            if not body.input.arrow_schema_json:
                raise ArtifactValidationError("input.schema_json is required for live execution")
            if body.input.rows is None:
                raise ArtifactValidationError("input.rows is required for live execution")
        except ArtifactValidationError as exc:
            raise HttpError(400, str(exc)) from exc

    with transaction.atomic():
        # Serialize quota and idempotency decisions within one tenant. The
        # matching database constraint protects the same invariant across
        # workers and process boundaries.
        Tenant.objects.select_for_update().get(id=principal.tenant.id)
        existing = Job.objects.filter(
            tenant_id=principal.tenant.id,
            operation_id=body.operation_id,
        ).first()
        if existing is not None:
            return InvokeResponse(
                job_id=existing.id,
                operation_id=existing.operation_id,
                status=existing.status,
                reused=True,
            )

        _enforce_concurrent_job_quota(principal.tenant.id)
        job = Job.objects.create(
            operation=body.operation,
            operation_id=body.operation_id,
            tenant_id=principal.tenant.id,
            input_payload_digest=body.input.payload_digest,
            kwargs_json={
                "params": body.params,
                "input_entity_ids": body.input_entity_ids,
                "input_schema_json": body.input.arrow_schema_json,
                "input_rows": body.input.rows,
                "input_artifact_key": body.input.artifact_key,
            },
            status=Job.Status.QUEUED,
        )
    async_result = dispatch_offload.delay(str(job.id))
    job.celery_task_id = async_result.id or ""
    job.save(update_fields=["celery_task_id"])
    return InvokeResponse(
        job_id=job.id,
        operation_id=job.operation_id,
        status=job.status,
        reused=False,
    )


@router.get("/{job_id}", response=JobStatusResponse)
def get_offload_job(request, job_id: UUID):
    principal = require_machine_key(request, scope=OFFLOAD_READ_SCOPE)
    job = get_object_or_404(Job, id=job_id, tenant_id=principal.tenant.id)
    output = None
    if job.status == Job.Status.SUCCEEDED and job.output_payload_digest:
        download_url = ""
        if job.output_artifact_key:
            try:
                store = _artifact_store_or_503()
                download_url = store.presign_get(principal.tenant.id, job.output_artifact_key)
            except ArtifactStorageError as exc:
                raise HttpError(503, "artifact storage unavailable") from exc
        output = ArrowBatchDescriptor(
            arrow_schema_json=job.output_schema_json,
            rows=job.output_rows,
            payload_digest=job.output_payload_digest,
            artifact_key=job.output_artifact_key,
            download_url=download_url,
        )
    return JobStatusResponse(
        job_id=job.id,
        operation=job.operation,
        operation_id=job.operation_id,
        status=job.status,
        output=output,
        error=job.error,
        logs=job.logs,
    )


@router.post("/{job_id}/cancel")
def cancel_offload_job(request, job_id: UUID):
    principal = require_machine_key(request, scope=OFFLOAD_CANCEL_SCOPE)
    job = get_object_or_404(Job, id=job_id, tenant_id=principal.tenant.id)
    if job.status in (Job.Status.SUCCEEDED, Job.Status.FAILED, Job.Status.CANCELED):
        return {"job_id": str(job.id), "status": job.status, "canceled": False}
    cancel_job_task.delay(str(job.id))
    job.status = Job.Status.CANCELED
    job.save(update_fields=["status", "updated_at"])
    return {"job_id": str(job.id), "status": job.status, "canceled": True}
