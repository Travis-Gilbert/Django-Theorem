"""Authenticated theorem.competence.v1 fit/refit/status/cleanup boundary.

W13 records truthful queued work and contract fixtures. The W14 worker is the
only component allowed to turn a queued job into a live fitted scorer.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from typing import Any
from uuid import UUID

from django.db import transaction
from ninja import Router

from apps.competence.contract import (
    COMPETENCE_EXCHANGE_SCHEMA,
    CompetenceCleanupReceipt,
    CompetenceCleanupRequest,
    CompetenceFitRequest,
    CompetenceJobStatus,
    CompetenceRefusal,
    CompetenceScorerArtifact,
    CompetenceSubmissionReceipt,
)
from apps.competence.models import CompetenceJob
from apps.competence.tasks import run_competence_fit
from apps.keys.auth import (
    COMPETENCE_CLEANUP_SCOPE,
    COMPETENCE_FIT_SCOPE,
    COMPETENCE_READ_SCOPE,
    require_machine_key,
)
from apps.orchestration.artifacts import (
    ArtifactConfigurationError,
    ArtifactStorageError,
    ArtifactStore,
    ArtifactValidationError,
    is_sha256_digest,
)
from apps.tenancy.models import Project, Tenant

router = Router(tags=["competence"])
logger = logging.getLogger(__name__)
CONTRACT_RESPONSES = {
    202: CompetenceSubmissionReceipt,
    400: CompetenceRefusal,
    403: CompetenceRefusal,
    404: CompetenceRefusal,
    409: CompetenceRefusal,
    422: CompetenceRefusal,
    500: CompetenceRefusal,
    503: CompetenceRefusal,
}


def canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def refusal(code: str, detail: str, *, retryable: bool = False) -> CompetenceRefusal:
    return CompetenceRefusal(code=code, detail=detail, retryable=retryable)


def _request_payload(
    body: CompetenceFitRequest | CompetenceCleanupRequest,
) -> dict[str, Any]:
    return body.model_dump(mode="json", exclude_none=True)


def _scope_payload(
    body: CompetenceFitRequest | CompetenceCleanupRequest,
) -> dict[str, Any]:
    return body.scope.model_dump(mode="json")


def _validate_fit_request(
    body: CompetenceFitRequest,
    *,
    expected_kind: str,
) -> CompetenceRefusal | None:
    if body.contract != COMPETENCE_EXCHANGE_SCHEMA:
        return refusal("unsupported_contract", f"unsupported contract {body.contract}")
    if body.message_kind != expected_kind:
        return refusal("message_kind_mismatch", f"expected {expected_kind}")
    if expected_kind == "fit_request" and body.previous_scorer is not None:
        return refusal("fit_has_previous_scorer", "fit cannot name a previous scorer")
    if expected_kind == "refit_request" and body.previous_scorer is None:
        return refusal(
            "refit_missing_previous_scorer", "refit requires a previous scorer"
        )
    if body.previous_scorer is not None and (
        not body.previous_scorer.scorer_id.strip()
        or not body.previous_scorer.scorer_version.strip()
        or not is_sha256_digest(body.previous_scorer.model_artifact_id)
        or not body.previous_scorer.prior_pack_id.strip()
    ):
        return refusal(
            "invalid_previous_scorer", "previous scorer identity is incomplete"
        )
    if not body.operation_id.strip() or len(body.operation_id) > 128:
        return refusal(
            "invalid_operation_id", "operation_id must contain 1 to 128 characters"
        )
    if not all(
        [
            body.scope.candidate_id.strip(),
            body.scope.candidate_lineage_id.strip(),
            body.evidence,
        ]
    ):
        return refusal("incomplete_scope", "candidate scope and evidence are required")
    if not all(
        is_sha256_digest(value)
        for value in [
            body.scope.pack_content_hash,
            body.training_corpus_digest,
            body.evaluation_corpus_digest,
        ]
    ):
        return refusal(
            "invalid_content_address",
            "package and corpus identities must be sha256 content addresses",
        )
    if body.training_corpus_digest == body.evaluation_corpus_digest:
        return refusal(
            "corpus_overlap", "training and evaluation corpora must be distinct"
        )
    if not math.isfinite(body.minimum_posterior_mean) or not (
        0.0 <= body.minimum_posterior_mean <= 1.0
    ):
        return refusal(
            "invalid_posterior_floor", "minimum_posterior_mean must be in [0, 1]"
        )
    if body.live_oracle_required and body.requested_oracle_class != "live":
        return refusal(
            "live_oracle_mismatch",
            "live_oracle_required requires requested_oracle_class=live",
        )

    lineages: set[str] = set()
    decision_refs: set[str] = set()
    source_refs: set[str] = set()
    roles: set[str] = set()
    for evidence in body.evidence:
        if (
            not evidence.causal_lineage_id.strip()
            or evidence.causal_lineage_id in lineages
            or not evidence.episode_ref.strip()
            or not is_sha256_digest(evidence.episode_digest)
            or not evidence.source_evidence_refs
            or evidence.source_evidence_refs
            != sorted(set(evidence.source_evidence_refs))
        ):
            return refusal(
                "invalid_evidence",
                "evidence must have unique lineages, content identity, and unique source refs",
            )
        lineages.add(evidence.causal_lineage_id)
        roles.add(evidence.role)
        source_refs.update(evidence.source_evidence_refs)
        if evidence.role == "held_out":
            selection = evidence.selection
            if selection is None:
                return refusal(
                    "missing_selection_evidence",
                    "held-out evidence requires W02 selection correction",
                )
            finite = [
                selection.behavior_probability,
                selection.target_probability,
                selection.importance_weight,
                selection.observed_outcome,
                selection.weighted_outcome,
            ]
            if (
                not selection.decision_ref.strip()
                or not 0.0 < selection.behavior_probability <= 1.0
                or not 0.0 <= selection.target_probability <= 1.0
                or not all(math.isfinite(value) for value in finite)
                or selection.importance_weight < 0.0
                or not 0.0 <= selection.observed_outcome <= 1.0
                or not 0.0 <= selection.weighted_outcome <= selection.importance_weight
                or not math.isclose(
                    selection.importance_weight,
                    selection.target_probability / selection.behavior_probability,
                    rel_tol=1e-12,
                    abs_tol=1e-12,
                )
                or not math.isclose(
                    selection.weighted_outcome,
                    selection.observed_outcome * selection.importance_weight,
                    rel_tol=1e-12,
                    abs_tol=1e-12,
                )
            ):
                return refusal(
                    "invalid_selection_evidence",
                    "W02 correction must be finite and have non-zero behavior support",
                )
            decision_refs.add(selection.decision_ref)
    if roles != {"training", "held_out"}:
        return refusal(
            "incomplete_corpora", "fit requires training and held-out evidence"
        )
    if body.w02_decision_refs != sorted(decision_refs):
        return refusal(
            "decision_ref_mismatch",
            "w02_decision_refs must exactly summarize held-out evidence",
        )
    if body.source_evidence_refs != sorted(source_refs):
        return refusal(
            "source_ref_mismatch",
            "source_evidence_refs must exactly summarize exported evidence",
        )
    return None


def _submission(job: CompetenceJob, *, reused: bool) -> CompetenceSubmissionReceipt:
    return CompetenceSubmissionReceipt(
        job_id=job.id,
        operation_id=job.operation_id,
        request_digest=job.request_digest,
        status=job.status,
        reused=reused,
    )


def _dispatch_competence_job(job_id: str) -> None:
    try:
        run_competence_fit.delay(job_id)
    # A broker outage leaves the durable queued job for the sleep-cycle sweep.
    except Exception:  # pragma: no cover
        logger.exception("competence job %s could not be dispatched", job_id)


def _submit(
    request,
    body: CompetenceFitRequest,
    *,
    expected_kind: str,
    operation: str,
):
    principal = require_machine_key(request, scope=COMPETENCE_FIT_SCOPE)
    problem = _validate_fit_request(body, expected_kind=expected_kind)
    if problem is not None:
        return 422, problem
    if body.scope.tenant_id != principal.tenant.id:
        return 403, refusal(
            "tenant_substitution_refused",
            "request tenant must equal the tenant derived from the verified machine key",
        )
    project = Project.objects.filter(
        id=body.scope.project_id,
        tenant_id=principal.tenant.id,
    ).first()
    if project is None:
        return 404, refusal(
            "project_not_found",
            "project does not exist inside the admitted tenant",
        )

    payload = _request_payload(body)
    request_digest = canonical_digest(payload)
    with transaction.atomic():
        Tenant.objects.select_for_update().get(id=principal.tenant.id)
        existing = CompetenceJob.objects.filter(
            tenant_id=principal.tenant.id,
            operation_id=body.operation_id,
        ).first()
        if existing is not None:
            if (
                existing.project_id != project.id
                or existing.operation != operation
                or existing.request_digest != request_digest
                or existing.request_json != payload
            ):
                return 409, refusal(
                    "conflicting_idempotency_key",
                    "operation_id is already bound to another request digest",
                )
            return 202, _submission(existing, reused=True)
        job = CompetenceJob.objects.create(
            tenant=principal.tenant,
            project=project,
            operation=operation,
            operation_id=body.operation_id,
            request_digest=request_digest,
            request_json=payload,
            status=CompetenceJob.Status.QUEUED,
        )
        transaction.on_commit(lambda: _dispatch_competence_job(str(job.id)))
    return 202, _submission(job, reused=False)


@router.post("/fit", response=CONTRACT_RESPONSES)
def fit_competence(request, body: CompetenceFitRequest):
    return _submit(
        request,
        body,
        expected_kind="fit_request",
        operation=CompetenceJob.Operation.FIT,
    )


@router.post("/refit", response=CONTRACT_RESPONSES)
def refit_competence(request, body: CompetenceFitRequest):
    return _submit(
        request,
        body,
        expected_kind="refit_request",
        operation=CompetenceJob.Operation.REFIT,
    )


STATUS_RESPONSES = {
    200: CompetenceJobStatus,
    404: CompetenceRefusal,
    500: CompetenceRefusal,
}


@router.get("/jobs/{job_id}", response=STATUS_RESPONSES)
def get_competence_job(request, job_id: UUID):
    principal = require_machine_key(request, scope=COMPETENCE_READ_SCOPE)
    job = CompetenceJob.objects.filter(id=job_id, tenant_id=principal.tenant.id).first()
    if job is None:
        return 404, refusal("job_not_found", "competence job was not found")
    scope = job.request_json.get("scope")
    if not isinstance(scope, dict):
        return 500, refusal("corrupt_job", "competence job is missing its scope")
    result = None
    if job.status == CompetenceJob.Status.SUCCEEDED:
        try:
            result = CompetenceScorerArtifact.model_validate(job.scorer_json)
        except ValueError:
            return 500, refusal(
                "incomplete_scorer",
                "succeeded competence job lacks a valid scorer artifact",
            )
    refused = None
    if job.status in {CompetenceJob.Status.REFUSED, CompetenceJob.Status.FAILED}:
        source = job.refusal_json or {
            "code": "job_failed",
            "detail": job.error or "competence job failed",
            "retryable": False,
        }
        refused = CompetenceRefusal.model_validate(source)
    return CompetenceJobStatus(
        job_id=job.id,
        operation_id=job.operation_id,
        request_digest=job.request_digest,
        status=job.status,
        scope=scope,
        result=result,
        refusal=refused,
    )


CLEANUP_RESPONSES = {
    200: CompetenceCleanupReceipt,
    400: CompetenceRefusal,
    403: CompetenceRefusal,
    404: CompetenceRefusal,
    409: CompetenceRefusal,
    503: CompetenceRefusal,
}


@router.post("/jobs/{job_id}/cleanup", response=CLEANUP_RESPONSES)
def cleanup_competence_job(request, job_id: UUID, body: CompetenceCleanupRequest):
    principal = require_machine_key(request, scope=COMPETENCE_CLEANUP_SCOPE)
    if body.contract != COMPETENCE_EXCHANGE_SCHEMA:
        return 400, refusal(
            "unsupported_contract", f"unsupported contract {body.contract}"
        )
    if body.job_id != job_id:
        return 400, refusal("job_id_mismatch", "path and body job_id must match")
    if body.scope.tenant_id != principal.tenant.id:
        return 403, refusal(
            "tenant_substitution_refused",
            "request tenant must equal the tenant derived from the verified machine key",
        )
    if (
        not body.cleanup_operation_id.strip()
        or not body.artifact_ids
        or body.artifact_ids != sorted(set(body.artifact_ids))
        or not all(is_sha256_digest(item) for item in body.artifact_ids)
    ):
        return 400, refusal(
            "invalid_cleanup_scope",
            "cleanup requires a canonical non-empty list of content-addressed artifacts",
        )

    with transaction.atomic():
        job = (
            CompetenceJob.objects.select_for_update()
            .filter(id=job_id, tenant_id=principal.tenant.id)
            .first()
        )
        if job is None:
            return 404, refusal("job_not_found", "competence job was not found")
        if (
            _scope_payload(body) != job.request_json.get("scope")
            or job.project_id != body.scope.project_id
        ):
            return 409, refusal(
                "cleanup_scope_mismatch",
                "cleanup scope does not match the exact competence job",
            )
        payload = _request_payload(body)
        request_digest = canonical_digest(payload)
        if job.cleanup_receipt_json:
            if job.cleanup_request_digest != request_digest:
                return 409, refusal(
                    "conflicting_cleanup_replay",
                    "job cleanup is already bound to another request digest",
                )
            receipt = CompetenceCleanupReceipt.model_validate(job.cleanup_receipt_json)
            return receipt.model_copy(update={"reused": True})

        artifact_keys = job.artifact_keys_json
        if not isinstance(artifact_keys, dict) or set(body.artifact_ids) != set(
            artifact_keys
        ):
            return 409, refusal(
                "artifact_ownership_mismatch",
                "cleanup artifacts do not exactly match this job's owned artifacts",
            )
        try:
            store = ArtifactStore.from_settings()
            for artifact_id in body.artifact_ids:
                artifact_key = artifact_keys.get(artifact_id)
                if not isinstance(artifact_key, str):
                    raise ArtifactValidationError(
                        "artifact ownership record is malformed"
                    )
                store.delete_competence_artifact(
                    principal.tenant.id,
                    job.project_id,
                    body.scope.candidate_id,
                    artifact_key,
                )
        except ArtifactConfigurationError as exc:
            return 503, refusal(
                "artifact_storage_unavailable", str(exc), retryable=True
            )
        except ArtifactStorageError:
            return 503, refusal(
                "artifact_cleanup_failed",
                "artifact storage could not complete cleanup",
                retryable=True,
            )
        except ArtifactValidationError as exc:
            return 409, refusal("artifact_ownership_mismatch", str(exc))

        receipt_payload = {
            "cleanup_operation_id": body.cleanup_operation_id,
            "request_digest": request_digest,
            "job_id": str(job.id),
            "scope": _scope_payload(body),
            "artifact_ids": body.artifact_ids,
        }
        receipt = CompetenceCleanupReceipt(
            receipt_id=canonical_digest(receipt_payload),
            cleanup_operation_id=body.cleanup_operation_id,
            request_digest=request_digest,
            job_id=job.id,
            scope=body.scope,
            artifact_ids=body.artifact_ids,
            cleaned=True,
            reused=False,
        )
        job.cleanup_request_digest = request_digest
        job.cleanup_receipt_json = receipt.model_dump(mode="json")
        job.save(
            update_fields=[
                "cleanup_request_digest",
                "cleanup_receipt_json",
                "updated_at",
            ]
        )
        return receipt
