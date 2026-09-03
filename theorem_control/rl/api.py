"""Tenant-admitted API for evaluation and prime-rl training runs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar
from uuid import UUID

from django.db import transaction
from django.db.models import Count
from django.shortcuts import get_object_or_404
from ninja import Field, Router, Schema, Status
from ninja.errors import HttpError

from apps.keys.auth import (
    RL_CANCEL_SCOPE,
    RL_EVAL_SCOPE,
    RL_READ_SCOPE,
    RL_TRAIN_SCOPE,
    require_machine_key,
)
from apps.orchestration.artifacts import (
    ArtifactConfigurationError,
    ArtifactStorageError,
    ArtifactStore,
    ArtifactValidationError,
    is_sha256_digest,
    sha256_digest,
)

from .ingest import config_digest
from .models import TrainingRun, is_immutable_image_reference
from .tasks import run_eval, run_prime_rl_training

router = Router(tags=["rl"])


class RunRequest(Schema):
    operation_id: str
    taskset_ref: str
    config: dict[str, Any] = Field(default_factory=dict)
    image_digest: str = ""

    model_config: ClassVar[dict[str, str]] = {"extra": "forbid"}


class SubmissionResponse(Schema):
    run_id: UUID
    status: str
    config_digest: str
    reused: bool


class ArtifactDescriptor(Schema):
    kind: str
    artifact_key: str
    payload_digest: str
    content_type: str
    download_url: str
    arrow_schema_json: str | None = None
    rows: int | None = None
    byte_length: int | None = None


class RunStatusResponse(Schema):
    run_id: UUID
    operation: str
    taskset_ref: str
    config_digest: str
    image_digest: str
    pod_id: str
    status: str
    artifacts: list[ArtifactDescriptor]
    trajectory_count: int
    error: str


def _assert_no_tenant_substitution(value: Any, tenant_id: UUID) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in {"tenant", "tenant_id"} and str(item) != str(tenant_id):
                raise HttpError(403, "payload tenant does not match the machine key")
            _assert_no_tenant_substitution(item, tenant_id)
    elif isinstance(value, list):
        for item in value:
            _assert_no_tenant_substitution(item, tenant_id)


def _validate_request(body: RunRequest, *, training: bool) -> None:
    if not body.operation_id.strip() or len(body.operation_id) > 128:
        raise HttpError(400, "operation_id must be between 1 and 128 characters")
    if not body.taskset_ref.strip() or len(body.taskset_ref) > 512:
        raise HttpError(400, "taskset_ref must be between 1 and 512 characters")
    if training and not is_immutable_image_reference(body.image_digest):
        raise HttpError(400, "training requires an immutable image_digest")


def _submit(
    request,
    body: RunRequest,
    *,
    operation: str,
    scope: str,
    task,
) -> Status[SubmissionResponse]:
    principal = require_machine_key(request, scope=scope)
    _assert_no_tenant_substitution(body.config, principal.tenant.id)
    training = operation == TrainingRun.Operation.PRIME_RL_TRAIN
    _validate_request(body, training=training)
    digest = config_digest(body.config)
    with transaction.atomic():
        existing = (
            TrainingRun.objects.select_for_update()
            .filter(
                tenant=principal.tenant,
                operation_id=body.operation_id,
            )
            .first()
        )
        if existing is not None:
            if (
                existing.operation != operation
                or existing.taskset_ref != body.taskset_ref
                or existing.config_digest != digest
                or existing.image_digest != body.image_digest
            ):
                raise HttpError(409, "operation_id was already used for another run")
            return Status(
                202,
                SubmissionResponse(
                    run_id=existing.id,
                    status=existing.status,
                    config_digest=existing.config_digest,
                    reused=True,
                ),
            )
        run = TrainingRun(
            tenant=principal.tenant,
            operation=operation,
            operation_id=body.operation_id,
            taskset_ref=body.taskset_ref,
            config_digest=digest,
            config_json=body.config,
            image_digest=body.image_digest,
        )
        run.full_clean()
        run.save()
        transaction.on_commit(lambda: task.delay(str(run.id)))
    return Status(
        202,
        SubmissionResponse(
            run_id=run.id,
            status=run.status,
            config_digest=run.config_digest,
            reused=False,
        ),
    )


@router.post("/eval", response={202: SubmissionResponse})
def submit_eval(request, body: RunRequest):
    return _submit(
        request,
        body,
        operation=TrainingRun.Operation.EVAL,
        scope=RL_EVAL_SCOPE,
        task=run_eval,
    )


@router.post("/train", response={202: SubmissionResponse})
def submit_training(request, body: RunRequest):
    return _submit(
        request,
        body,
        operation=TrainingRun.Operation.PRIME_RL_TRAIN,
        scope=RL_TRAIN_SCOPE,
        task=run_prime_rl_training,
    )


def _verified_artifacts(run: TrainingRun) -> list[ArtifactDescriptor]:
    if not run.artifact_descriptors:
        return []
    try:
        store = ArtifactStore.from_settings()
    except ArtifactConfigurationError as exc:
        raise HttpError(503, "artifact storage unavailable") from exc
    rendered = []
    for item in run.artifact_descriptors:
        key = item.get("artifact_key", "")
        digest = item.get("payload_digest", "")
        if not is_sha256_digest(digest):
            raise HttpError(500, "stored RL artifact descriptor is incomplete")
        try:
            if item.get("content_type") == "application/vnd.apache.arrow.stream":
                store.read_arrow(
                    run.tenant_id,
                    key,
                    expected_digest=digest,
                    expected_schema_json=item.get("schema_json", ""),
                    expected_rows=item.get("rows"),
                )
            elif sha256_digest(store.get_bytes(run.tenant_id, key)) != digest:
                raise ArtifactValidationError("artifact digest mismatch")
            rendered.append(
                ArtifactDescriptor(
                    kind=item["kind"],
                    artifact_key=key,
                    payload_digest=digest,
                    content_type=item["content_type"],
                    download_url=store.presign_get(run.tenant_id, key),
                    arrow_schema_json=item.get("schema_json"),
                    rows=item.get("rows"),
                    byte_length=item.get("byte_length"),
                )
            )
        except (ArtifactStorageError, ArtifactValidationError) as exc:
            raise HttpError(503, "artifact verification failed") from exc
    return rendered


@router.get("/runs/{run_id}", response=RunStatusResponse)
def get_run(request, run_id: UUID):
    principal = require_machine_key(request, scope=RL_READ_SCOPE)
    run = get_object_or_404(
        TrainingRun.objects.annotate(trajectory_count=Count("trajectories")),
        id=run_id,
        tenant=principal.tenant,
    )
    return RunStatusResponse(
        run_id=run.id,
        operation=run.operation,
        taskset_ref=run.taskset_ref,
        config_digest=run.config_digest,
        image_digest=run.image_digest,
        pod_id=run.pod_id,
        status=run.status,
        artifacts=_verified_artifacts(run),
        trajectory_count=run.trajectory_count,
        error=run.error,
    )


@router.post("/runs/{run_id}/cancel")
def cancel_run(request, run_id: UUID):
    principal = require_machine_key(request, scope=RL_CANCEL_SCOPE)
    with transaction.atomic():
        run = get_object_or_404(
            TrainingRun.objects.select_for_update(),
            id=run_id,
            tenant=principal.tenant,
        )
        if run.status in {
            TrainingRun.Status.SUCCEEDED,
            TrainingRun.Status.FAILED,
            TrainingRun.Status.CANCELED,
        }:
            return {"run_id": str(run.id), "status": run.status, "reused": True}
        run.status = TrainingRun.Status.CANCELED
        run.save(update_fields=["status", "updated_at"])
    run_prime_rl_training.delay(str(run.id))
    return {"run_id": str(run.id), "status": run.status, "reused": False}
