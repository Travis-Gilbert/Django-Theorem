"""Run the disposable hosted theorem.competence.v1 acceptance flow."""

from __future__ import annotations

import json
import time
from typing import Any
from uuid import uuid4

import httpx
from django.core.management.base import BaseCommand, CommandError

from apps.competence.contract import (
    CompetenceCleanupReceipt,
    CompetenceJobStatus,
    CompetenceSubmissionReceipt,
)
from apps.competence.models import CompetenceJob
from apps.keys.mint import mint_api_key
from apps.orchestration.artifacts import (
    ArtifactStorageError,
    ArtifactStore,
    sha256_digest,
)
from apps.tenancy.models import Project, Tenant

TERMINAL_STATES = {"succeeded", "failed", "refused", "canceled"}


def _live_request(tenant: Tenant, project: Project, run_id: str) -> dict[str, Any]:
    """Build named synthetic evidence for a real hosted execution-path oracle."""
    return {
        "contract": "theorem.competence.v1",
        "message_kind": "fit_request",
        "operation_id": f"competence-fit:live-smoke:{run_id}",
        "scope": {
            "tenant_id": str(tenant.id),
            "project_id": str(project.id),
            "pack_content_hash": f"sha256:{'a' * 64}",
            "candidate_id": f"candidate:live-smoke:{run_id}",
            "candidate_lineage_id": f"candidate-lineage:live-smoke:{run_id}",
        },
        "training_corpus_digest": f"sha256:{'c' * 64}",
        "evaluation_corpus_digest": f"sha256:{'b' * 64}",
        "evidence": [
            {
                "causal_lineage_id": f"practice-lineage:train:{run_id}",
                "role": "training",
                "episode_ref": f"episode:train:{run_id}",
                "episode_digest": f"sha256:{'1' * 64}",
                "survived": True,
                "source_evidence_refs": [
                    f"env-receipt:train:{run_id}",
                    f"practice-receipt:{run_id}",
                ],
            },
            {
                "causal_lineage_id": f"practice-lineage:held-out:{run_id}",
                "role": "held_out",
                "episode_ref": f"episode:held-out:{run_id}",
                "episode_digest": f"sha256:{'2' * 64}",
                "survived": True,
                "source_evidence_refs": [
                    f"env-receipt:held-out:{run_id}",
                    f"practice-receipt:{run_id}",
                    f"selection-receipt:{run_id}",
                ],
                "selection": {
                    "decision_ref": f"sha256:{'e' * 64}",
                    "policy_id": "ensemble.live-smoke.epsilon",
                    "policy_version": 1,
                    "seed": 7,
                    "behavior_probability": 0.5,
                    "target_probability": 1.0,
                    "importance_weight": 2.0,
                    "observed_outcome": 1.0,
                    "weighted_outcome": 2.0,
                },
            },
        ],
        "w02_decision_refs": [f"sha256:{'e' * 64}"],
        "source_evidence_refs": [
            f"env-receipt:held-out:{run_id}",
            f"env-receipt:train:{run_id}",
            f"practice-receipt:{run_id}",
            f"selection-receipt:{run_id}",
        ],
        "minimum_posterior_mean": 0.75,
        "requested_oracle_class": "deterministic_fixture",
        "substitution_allowed": True,
        "live_oracle_required": False,
    }


def _response_json(response: httpx.Response, expected_status: int) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise CommandError(
            f"hosted competence endpoint returned non-JSON HTTP {response.status_code}"
        ) from exc
    if not isinstance(payload, dict):
        raise CommandError("hosted competence endpoint returned a non-object payload")
    if response.status_code != expected_status:
        code = payload.get("code", "unexpected_response")
        detail = payload.get("detail", "hosted competence request failed")
        raise CommandError(f"{code}: {detail} (HTTP {response.status_code})")
    return payload


class Command(BaseCommand):
    help = "Run and exactly clean a disposable fit through the hosted API boundary."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--base-url", required=True)
        parser.add_argument("--poll-seconds", type=float, default=0.5)
        parser.add_argument("--timeout-seconds", type=float, default=90.0)
        parser.add_argument(
            "--confirm-live-cleanup",
            action="store_true",
            help="Required acknowledgement that disposable hosted objects are deleted.",
        )

    def handle(self, *args, **options) -> None:
        if not options["confirm_live_cleanup"]:
            raise CommandError("--confirm-live-cleanup is required")
        poll_seconds = options["poll_seconds"]
        timeout_seconds = options["timeout_seconds"]
        if poll_seconds <= 0 or timeout_seconds <= 0:
            raise CommandError("poll and timeout durations must be positive")

        base_url = options["base_url"].rstrip("/")
        if not base_url.startswith("https://"):
            raise CommandError("--base-url must use https://")

        run_id = uuid4().hex
        tenant = Tenant.objects.create(
            slug=f"competence-live-{run_id}",
            display_name="Disposable competence live smoke",
        )
        project = Project.objects.create(
            tenant=tenant,
            slug="competence-live-smoke",
            display_name="Disposable competence live smoke",
        )
        minted = mint_api_key(
            tenant,
            scopes=["competence:*"],
            label="disposable competence live smoke",
        )
        request_body = _live_request(tenant, project, run_id)
        job_id = ""
        api_cleaned = False
        receipt: dict[str, Any] | None = None

        try:
            headers = {"Authorization": f"Bearer {minted.plaintext}"}
            with httpx.Client(
                base_url=base_url,
                headers=headers,
                timeout=httpx.Timeout(20.0),
                follow_redirects=False,
            ) as client:
                submission_payload = _response_json(
                    client.post("/internal/competence/fit", json=request_body),
                    202,
                )
                submission = CompetenceSubmissionReceipt.model_validate(
                    submission_payload
                )
                job_id = str(submission.job_id)

                deadline = time.monotonic() + timeout_seconds
                status: CompetenceJobStatus | None = None
                while time.monotonic() < deadline:
                    status_payload = _response_json(
                        client.get(f"/internal/competence/jobs/{job_id}"),
                        200,
                    )
                    status = CompetenceJobStatus.model_validate(status_payload)
                    if status.status in TERMINAL_STATES:
                        break
                    time.sleep(poll_seconds)
                if status is None or status.status not in TERMINAL_STATES:
                    raise CommandError(
                        f"competence job {job_id} did not finish within {timeout_seconds}s"
                    )
                if status.status != "succeeded" or status.result is None:
                    refusal = status.refusal.code if status.refusal else "job_failed"
                    raise CommandError(
                        f"competence job {job_id} ended as {status.status}: {refusal}"
                    )

                job = CompetenceJob.objects.get(id=status.job_id, tenant=tenant)
                store = ArtifactStore.from_settings()
                artifacts = [
                    status.result.model_artifact,
                    status.result.prior_pack.artifact,
                ]
                for artifact in artifacts:
                    artifact_key = job.artifact_keys_json.get(artifact.artifact_id)
                    if not isinstance(artifact_key, str):
                        raise CommandError(
                            f"job omitted object key for {artifact.artifact_id}"
                        )
                    payload = store.get_bytes(tenant.id, artifact_key)
                    if len(payload) != artifact.byte_length:
                        raise CommandError(
                            f"artifact readback length mismatch for {artifact.artifact_id}"
                        )
                    if sha256_digest(payload) != artifact.payload_digest:
                        raise CommandError(
                            f"artifact readback digest mismatch for {artifact.artifact_id}"
                        )

                artifact_ids = sorted(item.artifact_id for item in artifacts)
                cleanup_body = {
                    "contract": "theorem.competence.v1",
                    "message_kind": "cleanup_request",
                    "cleanup_operation_id": f"competence-cleanup:live-smoke:{run_id}",
                    "job_id": job_id,
                    "scope": request_body["scope"],
                    "artifact_ids": artifact_ids,
                }
                cleanup_payload = _response_json(
                    client.post(
                        f"/internal/competence/jobs/{job_id}/cleanup",
                        json=cleanup_body,
                    ),
                    200,
                )
                cleanup = CompetenceCleanupReceipt.model_validate(cleanup_payload)
                replay_payload = _response_json(
                    client.post(
                        f"/internal/competence/jobs/{job_id}/cleanup",
                        json=cleanup_body,
                    ),
                    200,
                )
                replay = CompetenceCleanupReceipt.model_validate(replay_payload)
                if not cleanup.cleaned or cleanup.reused or not replay.reused:
                    raise CommandError("cleanup receipt replay semantics are invalid")
                api_cleaned = True

                for artifact_key in job.artifact_keys_json.values():
                    try:
                        store.get_bytes(tenant.id, artifact_key)
                    except ArtifactStorageError:
                        continue
                    raise CommandError(
                        f"artifact remained readable after cleanup: {artifact_key}"
                    )

                receipt = {
                    "schema": "theorem.competence-live-smoke.v1",
                    "boundary_evidence_class": "live",
                    "promotion_evidence_class": status.result.evidence_class,
                    "synthetic_evidence": True,
                    "auth_derived_tenant_verified": True,
                    "job_id": job_id,
                    "request_digest": status.request_digest,
                    "status": status.status,
                    "scorer_id": status.result.scorer_id,
                    "scorer_version": status.result.scorer_version,
                    "posterior_receipt_hash": status.result.posterior_receipt.receipt_hash,
                    "artifact_ids": artifact_ids,
                    "artifact_readback_verified": True,
                    "cleanup_receipt_id": cleanup.receipt_id,
                    "cleanup_replay_verified": True,
                    "artifact_deletion_verified": True,
                    "disposable_database_state_deleted": True,
                }
        finally:
            if not api_cleaned and job_id:
                job = CompetenceJob.objects.filter(id=job_id, tenant=tenant).first()
                if job is not None:
                    store = ArtifactStore.from_settings()
                    candidate_id = request_body["scope"]["candidate_id"]
                    for artifact_key in job.artifact_keys_json.values():
                        try:
                            store.delete_competence_artifact(
                                tenant.id,
                                project.id,
                                candidate_id,
                                artifact_key,
                            )
                        except ArtifactStorageError:
                            pass
            tenant.delete()

        if receipt is None:
            raise CommandError("live competence smoke completed without a receipt")
        self.stdout.write(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
