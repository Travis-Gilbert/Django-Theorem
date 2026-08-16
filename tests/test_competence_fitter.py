"""V14 known-truth, artifact, versioning, and recovery oracles."""

from __future__ import annotations

import copy
import json
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from django.test import Client, override_settings
from django.utils import timezone as dj_timezone

from apps.competence.api import canonical_digest
from apps.competence.contract import CompetenceFitRequest
from apps.competence.fitter import fit_competence
from apps.competence.models import CompetenceJob
from apps.competence.tasks import run_competence_fit, sweep_competence_jobs
from apps.keys.mint import mint_api_key
from apps.orchestration.artifacts import (
    ArtifactStore,
    ArtifactValidationError,
)
from apps.tenancy.models import Project, Tenant

FIXTURE_PATH = (
    Path(__file__).resolve().parents[1] / "contracts/theorem.competence.v1.fixture.json"
)


class ContentS3Client:
    def __init__(self):
        self.objects = {}
        self.puts = []

    def put_object(self, *, Bucket, Key, Body, ContentType):
        self.objects[(Bucket, Key)] = bytes(Body)
        self.puts.append((Bucket, Key, ContentType))
        return {}

    def get_object(self, *, Bucket, Key):
        payload = self.objects[(Bucket, Key)]
        return {"ContentLength": len(payload), "Body": BytesIO(payload)}

    def delete_object(self, *, Bucket, Key):
        self.objects.pop((Bucket, Key), None)
        return {}


@pytest.fixture
def contract_fixture():
    return json.loads(FIXTURE_PATH.read_text())


@pytest.fixture
def artifact_store():
    return ArtifactStore(
        endpoint_url="https://storage.example",
        access_key_id="access",
        secret_access_key="secret",
        region="us-east-2",
        bucket="competence",
        presign_seconds=300,
        max_bytes=1024 * 1024,
        client=ContentS3Client(),
    )


def admitted_client(contract_fixture, monkeypatch):
    scope = contract_fixture["fit_request"]["scope"]
    tenant = Tenant.objects.create(
        id=UUID(scope["tenant_id"]),
        slug=f"fitter-{uuid4()}",
        display_name="Fitter tenant",
    )
    project = Project.objects.create(
        id=UUID(scope["project_id"]),
        tenant=tenant,
        slug="fitter-project",
        display_name="Fitter project",
    )
    key = mint_api_key(tenant, scopes=["competence:*"])
    client = Client(HTTP_AUTHORIZATION=f"Bearer {key.plaintext}")
    monkeypatch.setattr(
        "apps.competence.api.run_competence_fit.delay",
        lambda _job_id: None,
    )
    return client, tenant, project


def post_json(client, path, value):
    return client.post(path, data=json.dumps(value), content_type="application/json")


def refit_request(fit_request, previous_scorer):
    request = copy.deepcopy(fit_request)
    request["message_kind"] = "refit_request"
    request["operation_id"] = "competence-refit:known-truth-v2"
    request["training_corpus_digest"] = "sha256:" + "3" * 64
    request["evaluation_corpus_digest"] = "sha256:" + "4" * 64
    request["previous_scorer"] = {
        "scorer_id": previous_scorer["scorer_id"],
        "scorer_version": previous_scorer["scorer_version"],
        "model_artifact_id": previous_scorer["model_artifact"]["artifact_id"],
        "prior_pack_id": previous_scorer["prior_pack"]["prior_pack_id"],
    }
    training, held_out = request["evidence"]
    training["causal_lineage_id"] = "practice-lineage:train-2"
    training["episode_ref"] = "episode:train-2"
    training["episode_digest"] = "sha256:" + "3" * 64
    training["source_evidence_refs"] = [
        "env-receipt:train-2",
        "practice-receipt:known-truth-v2",
    ]
    held_out["causal_lineage_id"] = "practice-lineage:held-out-2"
    held_out["episode_ref"] = "episode:held-out-2"
    held_out["episode_digest"] = "sha256:" + "4" * 64
    held_out["source_evidence_refs"] = [
        "env-receipt:held-out-2",
        "practice-receipt:known-truth-v2",
        "selection-receipt:known-truth-v2",
    ]
    held_out["selection"]["decision_ref"] = "sha256:" + "5" * 64
    request["w02_decision_refs"] = ["sha256:" + "5" * 64]
    request["source_evidence_refs"] = sorted(
        {
            source_ref
            for item in request["evidence"]
            for source_ref in item["source_evidence_refs"]
        }
    )
    return request


def test_known_truth_fit_is_deterministic_and_selection_corrected(contract_fixture):
    body = contract_fixture["fit_request"]
    request = CompetenceFitRequest.model_validate(body)
    request_digest = canonical_digest(body)

    first = fit_competence(request, request_digest)
    second = fit_competence(request, request_digest)

    assert first == second
    assert first.scorer.prior.model_dump() == {"alpha": 2.0, "beta": 1.0}
    assert first.scorer.posterior.model_dump() == {"alpha": 4.0, "beta": 1.0}
    assert first.scorer.accepted is True
    assert first.scorer.posterior_receipt.receipt_hash == (
        "8f366f9335b12414be620da2ebf260cf698c596101aa59c023e0949449f3fcdc"
    )


@pytest.mark.django_db
def test_api_dispatches_only_after_the_job_commit(
    contract_fixture,
    monkeypatch,
    django_capture_on_commit_callbacks,
):
    client, _, _ = admitted_client(contract_fixture, monkeypatch)
    dispatched = []
    monkeypatch.setattr(
        "apps.competence.api.run_competence_fit.delay",
        lambda job_id: dispatched.append(job_id),
    )

    with django_capture_on_commit_callbacks(execute=True):
        response = post_json(
            client,
            "/internal/competence/fit",
            contract_fixture["fit_request"],
        )
        assert response.status_code == 202
        assert CompetenceJob.objects.filter(id=response.json()["job_id"]).exists()
        assert dispatched == []

    assert dispatched == [response.json()["job_id"]]


@pytest.mark.django_db
def test_worker_publishes_verified_artifacts_before_success_and_replays(
    contract_fixture,
    artifact_store,
    monkeypatch,
):
    client, _, _ = admitted_client(contract_fixture, monkeypatch)
    response = post_json(
        client,
        "/internal/competence/fit",
        contract_fixture["fit_request"],
    )
    job_id = response.json()["job_id"]
    monkeypatch.setattr(
        "apps.competence.tasks.ArtifactStore.from_settings",
        lambda: artifact_store,
    )

    first = run_competence_fit(str(job_id))
    writes_after_first = len(artifact_store.client.puts)
    second = run_competence_fit(str(job_id))
    job = CompetenceJob.objects.get(id=job_id)
    status = client.get(f"/internal/competence/jobs/{job_id}")

    assert first["status"] == "succeeded"
    assert second == {"status": "succeeded", "joined": True}
    assert len(artifact_store.client.puts) == writes_after_first == 2
    assert job.status == CompetenceJob.Status.SUCCEEDED
    assert job.attempt_count == 1
    assert set(job.artifact_keys_json) == {
        job.scorer_json["model_artifact"]["artifact_id"],
        job.scorer_json["prior_pack"]["artifact"]["artifact_id"],
    }
    assert status.status_code == 200
    assert status.json()["result"] == job.scorer_json


@pytest.mark.django_db
def test_refit_consumes_exact_previous_version_without_lineage_reuse(
    contract_fixture,
    artifact_store,
    monkeypatch,
):
    client, _, _ = admitted_client(contract_fixture, monkeypatch)
    first_response = post_json(
        client,
        "/internal/competence/fit",
        contract_fixture["fit_request"],
    )
    monkeypatch.setattr(
        "apps.competence.tasks.ArtifactStore.from_settings",
        lambda: artifact_store,
    )
    run_competence_fit(first_response.json()["job_id"])
    first_job = CompetenceJob.objects.get(id=first_response.json()["job_id"])
    body = refit_request(contract_fixture["fit_request"], first_job.scorer_json)
    refit_response = post_json(client, "/internal/competence/refit", body)

    result = run_competence_fit(refit_response.json()["job_id"])
    refit_job = CompetenceJob.objects.get(id=refit_response.json()["job_id"])

    assert result["status"] == "succeeded"
    assert (
        refit_job.scorer_json["scorer_version"]
        != first_job.scorer_json["scorer_version"]
    )
    assert (
        refit_job.scorer_json["model_artifact"]["artifact_id"]
        != (first_job.scorer_json["model_artifact"]["artifact_id"])
    )
    assert refit_job.scorer_json["prior"]["alpha"] == (
        first_job.scorer_json["posterior"]["alpha"] + 1.0
    )
    assert len(artifact_store.client.objects) == 4

    overlapping = copy.deepcopy(body)
    overlapping["operation_id"] = "competence-refit:overlap"
    overlapping["evidence"] = copy.deepcopy(contract_fixture["fit_request"]["evidence"])
    overlapping["training_corpus_digest"] = "sha256:" + "6" * 64
    overlapping["evaluation_corpus_digest"] = "sha256:" + "7" * 64
    overlapping["w02_decision_refs"] = contract_fixture["fit_request"][
        "w02_decision_refs"
    ]
    overlapping["source_evidence_refs"] = contract_fixture["fit_request"][
        "source_evidence_refs"
    ]
    refused_response = post_json(client, "/internal/competence/refit", overlapping)
    refused = run_competence_fit(refused_response.json()["job_id"])

    assert refused == {
        "status": "refused",
        "code": "refit_lineage_overlap",
        "retryable": False,
    }


@pytest.mark.django_db
def test_partial_artifact_publication_never_claims_success(
    contract_fixture,
    artifact_store,
    monkeypatch,
):
    client, _, _ = admitted_client(contract_fixture, monkeypatch)
    response = post_json(
        client,
        "/internal/competence/fit",
        contract_fixture["fit_request"],
    )
    job_id = response.json()["job_id"]

    class FailingModelStore:
        def write_competence_artifact(self, *args, **kwargs):
            if args[3] == "scorer_model":
                raise ArtifactValidationError("model readback mismatch")
            return artifact_store.write_competence_artifact(*args, **kwargs)

    monkeypatch.setattr(
        "apps.competence.tasks.ArtifactStore.from_settings",
        lambda: FailingModelStore(),
    )
    result = run_competence_fit(str(job_id))
    job = CompetenceJob.objects.get(id=job_id)

    assert result["status"] == "failed"
    assert result["code"] == "artifact_publication_invalid"
    assert job.status == CompetenceJob.Status.FAILED
    assert job.scorer_json == {}
    assert len(job.artifact_keys_json) == 1


@pytest.mark.django_db
def test_worker_refuses_a_mutated_persisted_request(
    contract_fixture,
    artifact_store,
    monkeypatch,
):
    client, _, _ = admitted_client(contract_fixture, monkeypatch)
    response = post_json(
        client,
        "/internal/competence/fit",
        contract_fixture["fit_request"],
    )
    job = CompetenceJob.objects.get(id=response.json()["job_id"])
    mutated = copy.deepcopy(job.request_json)
    mutated["scope"]["candidate_id"] = "candidate:database-substitution"
    job.request_json = mutated
    job.save(update_fields=["request_json", "updated_at"])
    monkeypatch.setattr(
        "apps.competence.tasks.ArtifactStore.from_settings",
        lambda: artifact_store,
    )

    result = run_competence_fit(str(job.id))
    job.refresh_from_db()

    assert result == {
        "status": "refused",
        "code": "job_binding_mismatch",
        "retryable": False,
    }
    assert job.scorer_json == {}
    assert artifact_store.client.objects == {}


@pytest.mark.django_db
@override_settings(COMPETENCE_STALE_AFTER_SECONDS=60, COMPETENCE_SWEEP_BATCH_SIZE=10)
def test_sleep_cycle_recovers_abandoned_running_jobs(contract_fixture, monkeypatch):
    client, _, _ = admitted_client(contract_fixture, monkeypatch)
    response = post_json(
        client,
        "/internal/competence/fit",
        contract_fixture["fit_request"],
    )
    job_id = response.json()["job_id"]
    CompetenceJob.objects.filter(id=job_id).update(
        status=CompetenceJob.Status.RUNNING,
        celery_task_id="dead-worker",
        updated_at=dj_timezone.now() - timedelta(minutes=5),
    )
    dispatched = []
    monkeypatch.setattr(
        "apps.competence.tasks.run_competence_fit.delay",
        lambda recovered_id: dispatched.append(recovered_id),
    )

    result = sweep_competence_jobs()
    job = CompetenceJob.objects.get(id=job_id)

    assert result == {"recovered": 1, "dispatched": 1}
    assert job.status == CompetenceJob.Status.QUEUED
    assert job.celery_task_id == ""
    assert dispatched == [str(job_id)]
