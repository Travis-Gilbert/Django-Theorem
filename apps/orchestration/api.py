"""Internal offload API — D7 / D6 hard quota."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from django.shortcuts import get_object_or_404
from ninja import Field, Router, Schema
from ninja.errors import HttpError

from apps.billing.models import Subscription
from apps.orchestration.models import Job
from apps.orchestration.tasks import dispatch_offload, cancel_job_task

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

    model_config = {"populate_by_name": True}


class InvokeRequest(Schema):
    operation: str
    operation_id: str
    tenant_id: UUID | None = None
    input: ArrowBatchDescriptor
    input_entity_ids: list[str] = []
    params: dict[str, Any] = {}


class InvokeResponse(Schema):
    job_id: UUID
    operation_id: str
    status: str
    reused: bool = False


class JobStatusResponse(Schema):
    job_id: UUID
    operation: str
    operation_id: str
    status: str
    output: ArrowBatchDescriptor | None = None
    error: str = ""
    logs: str = ""


def _enforce_concurrent_job_quota(tenant_id: UUID | None) -> None:
    """Hard refuse at dispatch when queued|running jobs meet plan.limits.concurrent_jobs (D6/A7)."""
    if tenant_id is None:
        return
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


@router.post("/invoke", response=InvokeResponse)
def invoke_offload(request, body: InvokeRequest):
    if body.operation not in REGISTERED_OPERATIONS:
        raise HttpError(400, f"unknown operation: {body.operation}")

    existing = Job.objects.filter(operation_id=body.operation_id).first()
    if existing is not None:
        if existing.status in ACTIVE_JOB_STATUSES:
            return InvokeResponse(
                job_id=existing.id,
                operation_id=existing.operation_id,
                status=existing.status,
                reused=True,
            )
        if existing.status == Job.Status.SUCCEEDED:
            return InvokeResponse(
                job_id=existing.id,
                operation_id=existing.operation_id,
                status=existing.status,
                reused=True,
            )

    _enforce_concurrent_job_quota(body.tenant_id)

    job = Job.objects.create(
        operation=body.operation,
        operation_id=body.operation_id,
        tenant_id=body.tenant_id,
        input_payload_digest=body.input.payload_digest,
        kwargs_json={
            "params": body.params,
            "input_entity_ids": body.input_entity_ids,
            "input_schema_json": body.input.arrow_schema_json,
            "input_rows": body.input.rows,
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
    job = get_object_or_404(Job, id=job_id)
    output = None
    if job.status == Job.Status.SUCCEEDED and job.output_payload_digest:
        output = ArrowBatchDescriptor(
            arrow_schema_json=job.output_schema_json,
            rows=job.output_rows,
            payload_digest=job.output_payload_digest,
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
    job = get_object_or_404(Job, id=job_id)
    if job.status in (Job.Status.SUCCEEDED, Job.Status.FAILED, Job.Status.CANCELED):
        return {"job_id": str(job.id), "status": job.status, "canceled": False}
    cancel_job_task.delay(str(job.id))
    job.status = Job.Status.CANCELED
    job.save(update_fields=["status", "updated_at"])
    return {"job_id": str(job.id), "status": job.status, "canceled": True}
