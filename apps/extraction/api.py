"""Tenant-admitted internal extraction routes."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import timezone as datetime_timezone
from typing import Any
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from ninja import Field, Router, Schema
from ninja.errors import HttpError

from apps.keys.auth import (
    EXTRACTION_READ_SCOPE,
    EXTRACTION_REVIEW_SCOPE,
    EXTRACTION_SUBMIT_SCOPE,
    require_machine_key,
)
from apps.orchestration.artifacts import (
    ArtifactConfigurationError,
    ArtifactStorageError,
    ArtifactStore,
)
from apps.orchestration.tasks import cancel_job_task
from apps.tenancy.models import Tenant

from .models import ExtractionJob, ExtractionReview, ExtractionShard
from .reviews import is_candidate_digest
from .tasks import _contract, canonical_hash, submit_extraction


router = Router(tags=["extraction"])


class SubmitRequest(Schema):
    operation: str
    source_kind: str
    source_ref: dict[str, Any] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)


class SubmitResponse(Schema):
    job_id: UUID
    shard_count: int
    idempotent_replay: bool


class ArrowOutput(Schema):
    arrow_schema_json: str = Field(alias="schema_json")
    rows: int | None = None
    payload_digest: str
    artifact_key: str
    download_url: str

    model_config = {"populate_by_name": True}


class ShardStatus(Schema):
    index: int
    status: str
    output: ArrowOutput | None = None
    error: str = ""


class ExtractionStatus(Schema):
    job_id: UUID
    operation: str
    status: str
    shard_count: int
    rows_total: int
    shards: list[ShardStatus]


class ReviewItem(Schema):
    candidate_digest: str
    claim_id: str | None = None
    decision: str
    merge_target_claim_id: str | None = None
    reason: str = ""
    job_id: UUID | None = None


class ReviewResponse(Schema):
    created_ids: list[UUID]


def _canonical_object(value: Mapping[str, Any]) -> dict[str, Any]:
    return json.loads(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def _assert_tenant(value: Any, tenant_id: UUID) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in {"tenant", "tenant_id"} and str(item) != str(tenant_id):
                raise HttpError(403, "payload tenant does not match the machine key")
            _assert_tenant(item, tenant_id)
    elif isinstance(value, list):
        for item in value:
            _assert_tenant(item, tenant_id)


def _effective_params(body: SubmitRequest) -> dict[str, Any]:
    contract = _contract()
    params = _canonical_object(body.params)
    params.update(
        {
            "contract": contract["contract"],
            "model_id": params.get("model_id") or contract["model"]["default_id"],
            "prompt_hashes": {
                name: prompt["sha256"] for name, prompt in contract["prompts"].items()
            },
            "schema_hash": contract["schema_sha256"],
        }
    )
    return params


@router.post("/submit", response=SubmitResponse)
def submit(request, body: SubmitRequest):
    principal = require_machine_key(request, scope=EXTRACTION_SUBMIT_SCOPE)
    if body.operation not in ExtractionJob.Operation.values:
        raise HttpError(400, f"unsupported extraction operation: {body.operation}")
    if body.source_kind not in ExtractionJob.SourceKind.values:
        raise HttpError(400, f"unsupported extraction source: {body.source_kind}")
    _assert_tenant(body.source_ref, principal.tenant.id)
    _assert_tenant(body.params, principal.tenant.id)
    params = _effective_params(body)
    if body.operation == ExtractionJob.Operation.TYPED and not isinstance(
        params.get("object_type"), Mapping
    ):
        raise HttpError(400, "typed extraction requires params.object_type")
    source_ref = _canonical_object(body.source_ref)
    params_hash = canonical_hash(params)
    with transaction.atomic():
        Tenant.objects.select_for_update().get(id=principal.tenant.id)
        existing = ExtractionJob.objects.filter(
            tenant=principal.tenant,
            operation=body.operation,
            params_hash=params_hash,
            source_ref=source_ref,
        ).first()
        if existing is not None:
            return SubmitResponse(
                job_id=existing.id,
                shard_count=existing.shard_count,
                idempotent_replay=True,
            )
        job = ExtractionJob.objects.create(
            tenant=principal.tenant,
            operation=body.operation,
            source_kind=body.source_kind,
            source_ref=source_ref,
            params=params,
            params_hash=params_hash,
        )
    submit_extraction.delay(str(job.id))
    job.refresh_from_db()
    return SubmitResponse(
        job_id=job.id,
        shard_count=job.shard_count,
        idempotent_replay=False,
    )


def _store_or_503() -> ArtifactStore:
    try:
        return ArtifactStore.from_settings()
    except ArtifactConfigurationError as exc:
        raise HttpError(503, f"artifact storage unavailable: {exc}") from exc


@router.post("/review", response=ReviewResponse)
def review(request, body: list[ReviewItem]):
    principal = require_machine_key(request, scope=EXTRACTION_REVIEW_SCOPE)
    if len(body) > 500:
        raise HttpError(400, "review batch is limited to 500 items")
    created = []
    with transaction.atomic():
        for item in body:
            if not is_candidate_digest(item.candidate_digest):
                raise HttpError(400, "candidate_digest must be sha256")
            if item.decision not in ExtractionReview.Decision.values:
                raise HttpError(400, f"unsupported review decision: {item.decision}")
            job = None
            if item.job_id is not None:
                job = get_object_or_404(
                    ExtractionJob,
                    id=item.job_id,
                    tenant=principal.tenant,
                )
            decision = ExtractionReview(
                tenant=principal.tenant,
                job=job,
                candidate_digest=item.candidate_digest.removeprefix("sha256:"),
                claim_id=item.claim_id,
                decision=item.decision,
                merge_target_claim_id=item.merge_target_claim_id,
                reason=item.reason,
                reviewer=f"machine_key:{principal.api_key.id}",
            )
            try:
                decision.full_clean()
            except ValidationError as exc:
                raise HttpError(400, "; ".join(exc.messages)) from exc
            decision.save()
            created.append(decision.id)
    return ReviewResponse(created_ids=created)


@router.get("/review")
def reviews_since(request, since: str):
    principal = require_machine_key(request, scope=EXTRACTION_READ_SCOPE)
    parsed = parse_datetime(since)
    if parsed is None:
        raise HttpError(400, "since must be an ISO-8601 timestamp")
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, datetime_timezone.utc)
    decisions = ExtractionReview.objects.filter(
        tenant=principal.tenant,
        created_at__gte=parsed,
    ).order_by("created_at", "id")
    return [
        {
            "id": str(item.id),
            "candidate_digest": item.candidate_digest,
            "claim_id": item.claim_id,
            "decision": item.decision,
            "merge_target_claim_id": item.merge_target_claim_id,
            "reason": item.reason,
            "reviewer": item.reviewer,
            "created_at": item.created_at.isoformat(),
        }
        for item in decisions
    ]


@router.get("/{job_id}", response=ExtractionStatus)
def status(request, job_id: UUID):
    principal = require_machine_key(request, scope=EXTRACTION_READ_SCOPE)
    job = get_object_or_404(
        ExtractionJob.objects.prefetch_related("shards"),
        id=job_id,
        tenant=principal.tenant,
    )
    store = None
    shards = []
    for shard in job.shards.order_by("index"):
        output = None
        if shard.output_artifact_key and shard.output_digest:
            try:
                store = store or _store_or_503()
                download_url = store.presign_get(
                    principal.tenant.id,
                    shard.output_artifact_key,
                )
            except ArtifactStorageError as exc:
                raise HttpError(503, "artifact storage unavailable") from exc
            schema_json = (
                shard.orchestration_job.output_schema_json
                if shard.orchestration_job_id
                else ""
            )
            output = ArrowOutput(
                arrow_schema_json=schema_json,
                rows=shard.output_rows,
                payload_digest=shard.output_digest,
                artifact_key=shard.output_artifact_key,
                download_url=download_url,
            )
        shards.append(
            ShardStatus(
                index=shard.index,
                status=shard.status,
                output=output,
                error=shard.error,
            )
        )
    return ExtractionStatus(
        job_id=job.id,
        operation=job.operation,
        status=job.status,
        shard_count=job.shard_count,
        rows_total=job.rows_total,
        shards=shards,
    )


@router.post("/{job_id}/cancel")
def cancel(request, job_id: UUID):
    principal = require_machine_key(request, scope=EXTRACTION_SUBMIT_SCOPE)
    job = get_object_or_404(ExtractionJob, id=job_id, tenant=principal.tenant)
    terminal = {
        ExtractionJob.Status.SUCCEEDED,
        ExtractionJob.Status.FAILED,
        ExtractionJob.Status.CANCELED,
    }
    if job.status in terminal:
        return {"job_id": str(job.id), "status": job.status, "canceled": False}
    for shard in job.shards.select_related("orchestration_job"):
        if shard.orchestration_job_id and shard.status not in {
            ExtractionShard.Status.SUCCEEDED,
            ExtractionShard.Status.FAILED,
            ExtractionShard.Status.CANCELED,
            ExtractionShard.Status.SUPERSEDED,
        }:
            cancel_job_task.delay(str(shard.orchestration_job_id))
            shard.status = ExtractionShard.Status.CANCELED
            shard.save(update_fields=["status", "updated_at"])
    job.status = ExtractionJob.Status.CANCELED
    job.save(update_fields=["status", "updated_at"])
    return {"job_id": str(job.id), "status": job.status, "canceled": True}
