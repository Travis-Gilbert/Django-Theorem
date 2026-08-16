"""Celery execution and recovery for immutable competence fit/refit jobs."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone as dj_timezone
from pydantic import ValidationError

from apps.competence.contract import (
    COMPETENCE_EXCHANGE_SCHEMA,
    CompetenceFitRequest,
    CompetenceScorerArtifact,
    canonical_hex_digest,
)
from apps.competence.fitter import CompetenceFitRefusal, fit_competence
from apps.competence.models import CompetenceJob
from apps.orchestration.artifacts import (
    ArtifactConfigurationError,
    ArtifactStorageError,
    ArtifactStore,
    ArtifactValidationError,
)

logger = logging.getLogger(__name__)


def _task_identity(task, job_id: str) -> str:
    return str(task.request.id or f"eager:{job_id}")


def _claim_job(job_id: str, task_id: str) -> CompetenceJob | None:
    with transaction.atomic():
        job = CompetenceJob.objects.select_for_update().filter(id=job_id).first()
        if job is None:
            return None
        if job.status in {
            CompetenceJob.Status.SUCCEEDED,
            CompetenceJob.Status.REFUSED,
            CompetenceJob.Status.CANCELED,
        }:
            return job
        if (
            job.status == CompetenceJob.Status.RUNNING
            and job.celery_task_id
            and job.celery_task_id != task_id
        ):
            return job
        job.status = CompetenceJob.Status.RUNNING
        job.celery_task_id = task_id
        job.started_at = job.started_at or dj_timezone.now()
        job.attempt_count += 1
        job.error = ""
        job.refusal_json = {}
        job.save(
            update_fields=[
                "status",
                "celery_task_id",
                "started_at",
                "attempt_count",
                "error",
                "refusal_json",
                "updated_at",
            ]
        )
        return job


def _resolve_previous(
    job: CompetenceJob,
    request: CompetenceFitRequest,
) -> tuple[CompetenceScorerArtifact | None, CompetenceFitRequest | None]:
    if request.previous_scorer is None:
        return None, None
    expected = request.previous_scorer
    candidates = CompetenceJob.objects.filter(
        tenant_id=job.tenant_id,
        project_id=job.project_id,
        status=CompetenceJob.Status.SUCCEEDED,
    ).exclude(id=job.id)
    for candidate in candidates.iterator():
        try:
            scorer = CompetenceScorerArtifact.model_validate(candidate.scorer_json)
            previous_request = CompetenceFitRequest.model_validate(
                candidate.request_json
            )
        except ValidationError:
            continue
        if (
            scorer.scorer_id == expected.scorer_id
            and scorer.scorer_version == expected.scorer_version
            and scorer.model_artifact.artifact_id == expected.model_artifact_id
            and scorer.prior_pack.prior_pack_id == expected.prior_pack_id
        ):
            return scorer, previous_request
    raise CompetenceFitRefusal(
        "previous_scorer_not_found",
        "refit requires the exact prior scorer inside this tenant and project",
    )


def _validate_job_binding(job: CompetenceJob, request: CompetenceFitRequest) -> None:
    expected_kind = f"{job.operation}_request"
    request_digest = "sha256:" + canonical_hex_digest(
        request.model_dump(mode="json", exclude_none=True)
    )
    if (
        request.contract != COMPETENCE_EXCHANGE_SCHEMA
        or request.message_kind != expected_kind
        or request_digest != job.request_digest
        or request.scope.tenant_id != job.tenant_id
        or request.scope.project_id != job.project_id
    ):
        raise CompetenceFitRefusal(
            "job_binding_mismatch",
            "persisted competence request does not match its immutable job binding",
        )


def _record_artifact(job_id: str, artifact_id: str, artifact_key: str) -> None:
    with transaction.atomic():
        job = CompetenceJob.objects.select_for_update().get(id=job_id)
        artifacts = dict(job.artifact_keys_json)
        existing = artifacts.get(artifact_id)
        if existing is not None and existing != artifact_key:
            raise ArtifactValidationError(
                "artifact identity is already bound to a different object key"
            )
        artifacts[artifact_id] = artifact_key
        job.artifact_keys_json = artifacts
        job.save(update_fields=["artifact_keys_json", "updated_at"])


def _publish(job: CompetenceJob, fitted, store: ArtifactStore) -> dict[str, str]:
    scope = job.request_json["scope"]
    candidate_id = scope["candidate_id"]
    prior = store.write_competence_artifact(
        job.tenant_id,
        job.project_id,
        candidate_id,
        "prior_pack",
        fitted.prior_pack_payload,
        fitted.scorer.prior_pack.artifact.media_type,
    )
    if (
        prior.payload_digest != fitted.scorer.prior_pack.artifact.payload_digest
        or prior.byte_length != fitted.scorer.prior_pack.artifact.byte_length
    ):
        raise ArtifactValidationError(
            "published prior pack does not match its fitted descriptor"
        )
    _record_artifact(job.id, prior.payload_digest, prior.artifact_key)

    model = store.write_competence_artifact(
        job.tenant_id,
        job.project_id,
        candidate_id,
        "scorer_model",
        fitted.model_payload,
        fitted.scorer.model_artifact.media_type,
    )
    if (
        model.payload_digest != fitted.scorer.model_artifact.payload_digest
        or model.byte_length != fitted.scorer.model_artifact.byte_length
    ):
        raise ArtifactValidationError(
            "published scorer model does not match its fitted descriptor"
        )
    _record_artifact(job.id, model.payload_digest, model.artifact_key)
    return {
        prior.payload_digest: prior.artifact_key,
        model.payload_digest: model.artifact_key,
    }


def _finish_success(
    job_id: str,
    scorer: CompetenceScorerArtifact,
    artifact_keys: dict[str, str],
) -> dict[str, Any]:
    with transaction.atomic():
        job = CompetenceJob.objects.select_for_update().get(id=job_id)
        if job.status == CompetenceJob.Status.CANCELED:
            return {"status": "canceled"}
        if job.status == CompetenceJob.Status.SUCCEEDED:
            return {"status": "succeeded", "reused": True}
        if job.status != CompetenceJob.Status.RUNNING:
            return {"status": job.status, "joined": True}
        job.scorer_json = scorer.model_dump(mode="json")
        job.artifact_keys_json = artifact_keys
        job.status = CompetenceJob.Status.SUCCEEDED
        job.ended_at = dj_timezone.now()
        job.error = ""
        job.refusal_json = {}
        job.save(
            update_fields=[
                "scorer_json",
                "artifact_keys_json",
                "status",
                "ended_at",
                "error",
                "refusal_json",
                "updated_at",
            ]
        )
    return {
        "status": "succeeded",
        "scorer_id": scorer.scorer_id,
        "scorer_version": scorer.scorer_version,
        "model_artifact_id": scorer.model_artifact.artifact_id,
        "prior_pack_id": scorer.prior_pack.prior_pack_id,
    }


def _finish_problem(
    job_id: str,
    *,
    status: str,
    code: str,
    detail: str,
    retryable: bool,
) -> dict[str, Any]:
    with transaction.atomic():
        job = CompetenceJob.objects.select_for_update().get(id=job_id)
        if job.status == CompetenceJob.Status.CANCELED:
            return {"status": "canceled"}
        job.status = status
        job.error = detail
        job.refusal_json = {
            "code": code,
            "detail": detail,
            "retryable": retryable,
        }
        job.ended_at = dj_timezone.now()
        job.save(
            update_fields=[
                "status",
                "error",
                "refusal_json",
                "ended_at",
                "updated_at",
            ]
        )
    return {"status": status, "code": code, "retryable": retryable}


@shared_task(
    bind=True,
    name="apps.competence.tasks.run_competence_fit",
    max_retries=3,
)
def run_competence_fit(self, job_id: str) -> dict[str, Any]:
    """Fit and publish one immutable scorer, replaying safely by job identity."""
    task_id = _task_identity(self, job_id)
    job = _claim_job(job_id, task_id)
    if job is None:
        return {"status": "missing"}
    if job.status != CompetenceJob.Status.RUNNING:
        return {"status": job.status, "joined": True}

    try:
        request = CompetenceFitRequest.model_validate(job.request_json)
        _validate_job_binding(job, request)
        previous_scorer, previous_request = _resolve_previous(job, request)
        fitted = fit_competence(
            request,
            job.request_digest,
            previous_scorer=previous_scorer,
            previous_request=previous_request,
        )
        store = ArtifactStore.from_settings()
        artifact_keys = _publish(job, fitted, store)
        return _finish_success(job_id, fitted.scorer, artifact_keys)
    except CompetenceFitRefusal as exc:
        return _finish_problem(
            job_id,
            status=CompetenceJob.Status.REFUSED,
            code=exc.code,
            detail=exc.detail,
            retryable=False,
        )
    except ValidationError as exc:
        return _finish_problem(
            job_id,
            status=CompetenceJob.Status.REFUSED,
            code="invalid_persisted_request",
            detail=str(exc)[:2_000],
            retryable=False,
        )
    except (ArtifactConfigurationError, ArtifactStorageError) as exc:
        if self.request.retries < self.max_retries:
            raise self.retry(
                exc=exc,
                countdown=min(2 ** (self.request.retries + 1), 60),
            )
        return _finish_problem(
            job_id,
            status=CompetenceJob.Status.FAILED,
            code="artifact_storage_unavailable",
            detail=str(exc),
            retryable=True,
        )
    except ArtifactValidationError as exc:
        return _finish_problem(
            job_id,
            status=CompetenceJob.Status.FAILED,
            code="artifact_publication_invalid",
            detail=str(exc),
            retryable=False,
        )
    except Exception as exc:  # pragma: no cover - defensive worker boundary
        logger.exception("competence fit %s failed unexpectedly", job_id)
        return _finish_problem(
            job_id,
            status=CompetenceJob.Status.FAILED,
            code="competence_fit_failed",
            detail=str(exc)[:2_000],
            retryable=False,
        )


@shared_task(name="apps.competence.tasks.sweep_competence_jobs")
def sweep_competence_jobs() -> dict[str, int]:
    """Sleep-cycle recovery for queued or abandoned competence jobs."""
    cutoff = dj_timezone.now() - timedelta(
        seconds=settings.COMPETENCE_STALE_AFTER_SECONDS
    )
    stale_ids = list(
        CompetenceJob.objects.filter(
            status=CompetenceJob.Status.RUNNING,
            updated_at__lt=cutoff,
        ).values_list("id", flat=True)
    )
    with transaction.atomic():
        recovered = (
            CompetenceJob.objects.select_for_update()
            .filter(
                id__in=stale_ids,
                status=CompetenceJob.Status.RUNNING,
                updated_at__lt=cutoff,
            )
            .update(
                status=CompetenceJob.Status.QUEUED,
                celery_task_id="",
            )
        )
    queued_ids = list(
        CompetenceJob.objects.filter(status=CompetenceJob.Status.QUEUED)
        .order_by("created_at")
        .values_list("id", flat=True)[: settings.COMPETENCE_SWEEP_BATCH_SIZE]
    )
    for queued_id in queued_ids:
        run_competence_fit.delay(str(queued_id))
    return {"recovered": recovered, "dispatched": len(queued_ids)}
