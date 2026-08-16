"""V13 contract oracles for theorem.competence.v1.

The succeeded scorer installed below is explicitly a deterministic fixture. It
proves wire compatibility only; it is not evidence that W14 fitted a model.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from django.test import Client
from pydantic import ValidationError

from apps.competence.api import canonical_digest
from apps.competence.contract import (
    COMPETENCE_EXCHANGE_FIXTURE_DIGEST,
    CompetenceCleanupReceipt,
    CompetenceCleanupRequest,
    CompetenceFitRequest,
    CompetenceJobStatus,
    CompetenceRefusal,
    CompetenceSubmissionReceipt,
)
from apps.competence.models import CompetenceJob
from apps.keys.mint import mint_api_key
from apps.tenancy.models import Project, Tenant

FIXTURE_PATH = (
    Path(__file__).resolve().parents[1] / "contracts/theorem.competence.v1.fixture.json"
)


@pytest.fixture
def contract_fixture():
    return json.loads(FIXTURE_PATH.read_text())


@pytest.fixture
def competence_client(db, contract_fixture):
    scope = contract_fixture["fit_request"]["scope"]
    tenant = Tenant.objects.create(
        id=UUID(scope["tenant_id"]),
        slug=f"competence-{uuid4()}",
        display_name="Competence tenant",
    )
    project = Project.objects.create(
        id=UUID(scope["project_id"]),
        tenant=tenant,
        slug="competence-project",
        display_name="Competence project",
    )
    minted = mint_api_key(tenant, scopes=["competence:*"])
    client = Client(HTTP_AUTHORIZATION=f"Bearer {minted.plaintext}")
    client.tenant = tenant
    client.project = project
    return client


def post_json(client, path, value):
    return client.post(path, data=json.dumps(value), content_type="application/json")


def install_deterministic_competence_fixture(job, contract_fixture, artifact_store):
    """Install a named W13 stand-in; never use this as a W14 live oracle."""
    scorer = copy.deepcopy(contract_fixture["succeeded_status"]["result"])
    scope = contract_fixture["fit_request"]["scope"]
    artifacts = [scorer["model_artifact"], scorer["prior_pack"]["artifact"]]
    job.scorer_json = scorer
    job.artifact_keys_json = {
        artifact["artifact_id"]: artifact_store.competence_artifact_key(
            job.tenant_id,
            job.project_id,
            scope["candidate_id"],
            artifact["kind"],
            artifact["payload_digest"],
        )
        for artifact in artifacts
    }
    job.status = CompetenceJob.Status.SUCCEEDED
    job.save(
        update_fields=["scorer_json", "artifact_keys_json", "status", "updated_at"]
    )


@pytest.mark.django_db
def test_versioned_fixture_is_strict_and_round_trips_without_semantic_loss(
    contract_fixture,
):
    assert canonical_digest(contract_fixture) == COMPETENCE_EXCHANGE_FIXTURE_DIGEST
    models = {
        "fit_request": CompetenceFitRequest,
        "refit_request": CompetenceFitRequest,
        "submission_receipt": CompetenceSubmissionReceipt,
        "queued_status": CompetenceJobStatus,
        "succeeded_status": CompetenceJobStatus,
        "refusal": CompetenceRefusal,
        "cleanup_request": CompetenceCleanupRequest,
        "cleanup_receipt": CompetenceCleanupReceipt,
    }
    for name, schema in models.items():
        parsed = schema.model_validate(contract_fixture[name])
        assert (
            parsed.model_dump(mode="json", exclude_none=True) == contract_fixture[name]
        )

    unknown = copy.deepcopy(contract_fixture["fit_request"])
    unknown["tenant_authority"] = "caller-selected"
    with pytest.raises(ValidationError):
        CompetenceFitRequest.model_validate(unknown)


@pytest.mark.django_db
def test_fit_is_authenticated_queued_inspectable_and_exactly_idempotent(
    competence_client,
    contract_fixture,
):
    body = contract_fixture["fit_request"]
    first = post_json(competence_client, "/internal/competence/fit", body)
    second = post_json(competence_client, "/internal/competence/fit", body)

    assert first.status_code == 202, first.content
    assert second.status_code == 202, second.content
    assert first.json()["status"] == "queued"
    assert first.json()["reused"] is False
    assert second.json()["reused"] is True
    assert second.json()["job_id"] == first.json()["job_id"]
    assert first.json()["request_digest"] == canonical_digest(body)
    assert (
        first.json()["request_digest"]
        == contract_fixture["submission_receipt"]["request_digest"]
    )
    assert CompetenceJob.objects.count() == 1

    status = competence_client.get(
        f"/internal/competence/jobs/{first.json()['job_id']}"
    )
    assert status.status_code == 200, status.content
    assert status.json()["status"] == "queued"
    assert "result" not in status.json() or status.json()["result"] is None


@pytest.mark.django_db
def test_conflicting_key_reuse_fails_closed(competence_client, contract_fixture):
    body = contract_fixture["fit_request"]
    first = post_json(competence_client, "/internal/competence/fit", body)
    conflicting = copy.deepcopy(body)
    conflicting["scope"]["candidate_id"] = "candidate:conflict"
    second = post_json(competence_client, "/internal/competence/fit", conflicting)

    assert first.status_code == 202
    assert second.status_code == 409
    assert second.json()["code"] == "conflicting_idempotency_key"
    assert CompetenceJob.objects.count() == 1


@pytest.mark.django_db
def test_verified_key_derives_tenant_and_refuses_caller_substitution(
    competence_client,
    contract_fixture,
):
    other = Tenant.objects.create(slug=f"other-{uuid4()}", display_name="Other")
    other_key = mint_api_key(other, scopes=["competence:fit"])
    other_client = Client(HTTP_AUTHORIZATION=f"Bearer {other_key.plaintext}")

    response = post_json(
        other_client, "/internal/competence/fit", contract_fixture["fit_request"]
    )

    assert response.status_code == 403
    assert response.json()["code"] == "tenant_substitution_refused"
    assert CompetenceJob.objects.count() == 0


@pytest.mark.django_db
def test_machine_key_and_scope_are_required(contract_fixture, competence_client):
    unauthenticated = post_json(
        Client(), "/internal/competence/fit", contract_fixture["fit_request"]
    )
    assert unauthenticated.status_code == 401

    weak_key = mint_api_key(competence_client.tenant, scopes=["competence:read"])
    weak_client = Client(HTTP_AUTHORIZATION=f"Bearer {weak_key.plaintext}")
    forbidden = post_json(
        weak_client, "/internal/competence/fit", contract_fixture["fit_request"]
    )
    assert forbidden.status_code == 403
    assert CompetenceJob.objects.count() == 0


@pytest.mark.django_db
def test_refit_requires_and_preserves_previous_scorer(
    competence_client, contract_fixture
):
    response = post_json(
        competence_client,
        "/internal/competence/refit",
        contract_fixture["refit_request"],
    )

    assert response.status_code == 202, response.content
    job = CompetenceJob.objects.get(id=response.json()["job_id"])
    assert job.operation == CompetenceJob.Operation.REFIT
    assert (
        job.request_json["previous_scorer"]
        == contract_fixture["refit_request"]["previous_scorer"]
    )


@pytest.mark.django_db
def test_status_is_tenant_isolated_and_scorer_fields_round_trip(
    competence_client,
    contract_fixture,
    monkeypatch,
):
    response = post_json(
        competence_client,
        "/internal/competence/fit",
        contract_fixture["fit_request"],
    )
    job = CompetenceJob.objects.get(id=response.json()["job_id"])

    class FixtureArtifactStore:
        def competence_artifact_key(
            self,
            tenant_id,
            project_id,
            candidate_id,
            artifact_kind,
            payload_digest,
        ):
            candidate = candidate_id.replace(":", "-")
            return (
                f"tenants/{tenant_id}/projects/{project_id}/competence/"
                f"{candidate}/{artifact_kind}/{payload_digest.removeprefix('sha256:')}"
            )

    install_deterministic_competence_fixture(
        job, contract_fixture, FixtureArtifactStore()
    )
    status = competence_client.get(f"/internal/competence/jobs/{job.id}")

    assert status.status_code == 200, status.content
    assert status.json()["status"] == "succeeded"
    assert status.json()["result"] == contract_fixture["succeeded_status"]["result"]

    other = Tenant.objects.create(slug=f"isolated-{uuid4()}", display_name="Isolated")
    other_key = mint_api_key(other, scopes=["competence:read"])
    hidden = Client(HTTP_AUTHORIZATION=f"Bearer {other_key.plaintext}").get(
        f"/internal/competence/jobs/{job.id}"
    )
    assert hidden.status_code == 404


@pytest.mark.django_db
def test_succeeded_status_refuses_non_content_addressed_scorer(
    competence_client,
    contract_fixture,
):
    response = post_json(
        competence_client,
        "/internal/competence/fit",
        contract_fixture["fit_request"],
    )
    job = CompetenceJob.objects.get(id=response.json()["job_id"])
    scorer = copy.deepcopy(contract_fixture["succeeded_status"]["result"])
    scorer["model_artifact"]["artifact_id"] = f"sha256:{'0' * 64}"
    job.scorer_json = scorer
    job.status = CompetenceJob.Status.SUCCEEDED
    job.save(update_fields=["scorer_json", "status", "updated_at"])

    status = competence_client.get(f"/internal/competence/jobs/{job.id}")

    assert status.status_code == 500
    assert status.json()["code"] == "incomplete_scorer"


@pytest.mark.django_db
def test_cleanup_is_exact_auditable_idempotent_and_cannot_cross_candidate(
    competence_client,
    contract_fixture,
    monkeypatch,
):
    response = post_json(
        competence_client,
        "/internal/competence/fit",
        contract_fixture["fit_request"],
    )
    job = CompetenceJob.objects.get(id=response.json()["job_id"])

    class RecordingArtifactStore:
        def __init__(self):
            self.deleted = []

        def competence_artifact_key(
            self,
            tenant_id,
            project_id,
            candidate_id,
            artifact_kind,
            payload_digest,
        ):
            return (
                f"tenants/{tenant_id}/projects/{project_id}/competence/"
                f"candidate-fixture-v1/{artifact_kind}/{payload_digest.removeprefix('sha256:')}"
            )

        def delete_competence_artifact(
            self,
            tenant_id,
            project_id,
            candidate_id,
            artifact_key,
        ):
            assert tenant_id == competence_client.tenant.id
            assert project_id == competence_client.project.id
            assert candidate_id == "candidate:fixture-v1"
            assert artifact_key.startswith(
                f"tenants/{tenant_id}/projects/{project_id}/"
            )
            self.deleted.append(artifact_key)

    store = RecordingArtifactStore()
    install_deterministic_competence_fixture(job, contract_fixture, store)
    monkeypatch.setattr(
        "apps.competence.api.ArtifactStore.from_settings", lambda: store
    )
    cleanup = copy.deepcopy(contract_fixture["cleanup_request"])
    cleanup["job_id"] = str(job.id)

    first = post_json(
        competence_client,
        f"/internal/competence/jobs/{job.id}/cleanup",
        cleanup,
    )
    second = post_json(
        competence_client,
        f"/internal/competence/jobs/{job.id}/cleanup",
        cleanup,
    )

    assert first.status_code == 200, first.content
    assert first.json()["cleaned"] is True
    assert first.json()["reused"] is False
    assert second.status_code == 200, second.content
    assert second.json()["receipt_id"] == first.json()["receipt_id"]
    assert second.json()["reused"] is True
    assert len(store.deleted) == 2

    other_tenant = Tenant.objects.create(
        slug=f"cleanup-other-{uuid4()}", display_name="Other"
    )
    other_key = mint_api_key(other_tenant, scopes=["competence:cleanup"])
    cross_tenant = post_json(
        Client(HTTP_AUTHORIZATION=f"Bearer {other_key.plaintext}"),
        f"/internal/competence/jobs/{job.id}/cleanup",
        cleanup,
    )
    assert cross_tenant.status_code == 403
    assert cross_tenant.json()["code"] == "tenant_substitution_refused"
    assert len(store.deleted) == 2

    other_candidate = copy.deepcopy(cleanup)
    other_candidate["cleanup_operation_id"] = "competence-cleanup:other-candidate"
    other_candidate["scope"]["candidate_id"] = "candidate:other"
    conflict = post_json(
        competence_client,
        f"/internal/competence/jobs/{job.id}/cleanup",
        other_candidate,
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "cleanup_scope_mismatch"
    assert len(store.deleted) == 2


@pytest.mark.django_db
def test_duplicate_lineage_and_raw_authority_fields_are_refused(
    competence_client,
    contract_fixture,
):
    duplicate = copy.deepcopy(contract_fixture["fit_request"])
    duplicate["evidence"].append(copy.deepcopy(duplicate["evidence"][0]))
    response = post_json(competence_client, "/internal/competence/fit", duplicate)
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_evidence"

    authority = copy.deepcopy(contract_fixture["fit_request"])
    authority["graph_authority"] = {"can_promote": True}
    unknown = post_json(competence_client, "/internal/competence/fit", authority)
    assert unknown.status_code == 422

    false_live = copy.deepcopy(contract_fixture["fit_request"])
    false_live["live_oracle_required"] = True
    not_live = post_json(competence_client, "/internal/competence/fit", false_live)
    assert not_live.status_code == 422
    assert not_live.json()["code"] == "live_oracle_mismatch"
    assert CompetenceJob.objects.count() == 0
