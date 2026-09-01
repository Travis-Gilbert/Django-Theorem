"""A8 — offload invoke / status / cancel + operation_id idempotency + D6 quota."""

from __future__ import annotations

import json
import uuid

import pytest
from django.test import Client, override_settings

from apps.billing.models import Plan, Subscription
from apps.keys.mint import mint_api_key
from apps.orchestration.models import Job
from apps.tenancy.models import Tenant


@pytest.fixture
def client(db):
    """Authenticated tenant client; authorization is part of every API test."""
    tenant = Tenant.objects.create(slug=f"offload-{uuid.uuid4()}", display_name="Offload")
    minted = mint_api_key(tenant, scopes=["offload:*"])
    client = Client()
    client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {minted.plaintext}"
    client.tenant = tenant
    return client


@pytest.mark.django_db
@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
def test_invoke_creates_job_and_completes(client):
    op_id = f"op-{uuid.uuid4()}"
    body = {
        "operation": "data_science.gnn.embed",
        "operation_id": op_id,
        "input": {
            "schema_json": '{"fields":[]}',
            "rows": 0,
            "payload_digest": "abc123",
        },
        "input_entity_ids": [],
        "params": {"dim": 32},
    }
    resp = client.post(
        "/internal/offload/invoke",
        data=json.dumps(body),
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    data = resp.json()
    assert data["operation_id"] == op_id
    assert data["reused"] is False
    job = Job.objects.get(id=data["job_id"])
    assert job.status == Job.Status.SUCCEEDED
    assert job.output_payload_digest

    status = client.get(f"/internal/offload/{job.id}")
    assert status.status_code == 200
    assert status.json()["status"] == "succeeded"
    assert status.json()["output"]["payload_digest"] == job.output_payload_digest


@pytest.mark.django_db
@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
def test_invoke_idempotent_on_operation_id(client):
    op_id = f"op-{uuid.uuid4()}"
    body = {
        "operation": "data_science.community.assign",
        "operation_id": op_id,
        "input": {"schema_json": "{}", "rows": 1, "payload_digest": "d1"},
    }
    r1 = client.post(
        "/internal/offload/invoke",
        data=json.dumps(body),
        content_type="application/json",
    )
    assert r1.status_code == 200
    # Second dispatch while first succeeded — reuse
    r2 = client.post(
        "/internal/offload/invoke",
        data=json.dumps(body),
        content_type="application/json",
    )
    assert r2.status_code == 200
    assert r2.json()["reused"] is True
    assert r2.json()["job_id"] == r1.json()["job_id"]
    assert Job.objects.filter(operation_id=op_id).count() == 1


@pytest.mark.django_db
@override_settings(CELERY_TASK_ALWAYS_EAGER=False)
def test_operation_id_is_idempotent_per_tenant(client):
    """A caller cannot consume another tenant's idempotency namespace."""
    op_id = f"op-shared-{uuid.uuid4()}"
    other = Tenant.objects.create(slug=f"other-{uuid.uuid4()}", display_name="Other")
    other_key = mint_api_key(other, scopes=["offload:invoke"])
    body = {
        "operation": "data_science.community.assign",
        "operation_id": op_id,
        "input": {"schema_json": "{}", "rows": 1, "payload_digest": "d1"},
    }

    first = client.post("/internal/offload/invoke", data=json.dumps(body), content_type="application/json")
    second = Client().post(
        "/internal/offload/invoke",
        data=json.dumps(body),
        content_type="application/json",
        headers={"Authorization": f"Bearer {other_key.plaintext}"},
    )

    assert first.status_code == 200, first.content
    assert second.status_code == 200, second.content
    assert first.json()["reused"] is False
    assert second.json()["reused"] is False
    assert Job.objects.filter(operation_id=op_id).count() == 2


@pytest.mark.django_db
@override_settings(CELERY_TASK_ALWAYS_EAGER=False)
def test_invoke_concurrent_idempotency_while_queued(client):
    """A8-DJ: same operation_id while first still queued returns same job_id (DB unique)."""
    op_id = f"op-queued-{uuid.uuid4()}"
    existing = Job.objects.create(
        tenant=client.tenant,
        operation="data_science.gnn.denoise",
        operation_id=op_id,
        status=Job.Status.QUEUED,
    )
    body = {
        "operation": "data_science.gnn.denoise",
        "operation_id": op_id,
        "input": {"schema_json": "{}", "rows": 0, "payload_digest": "x"},
    }
    r1 = client.post(
        "/internal/offload/invoke",
        data=json.dumps(body),
        content_type="application/json",
    )
    r2 = client.post(
        "/internal/offload/invoke",
        data=json.dumps(body),
        content_type="application/json",
    )
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["job_id"] == str(existing.id)
    assert r2.json()["job_id"] == str(existing.id)
    assert r1.json()["reused"] is True
    assert r2.json()["reused"] is True
    assert Job.objects.filter(operation_id=op_id).count() == 1


@pytest.mark.django_db
@override_settings(CELERY_TASK_ALWAYS_EAGER=False)
def test_invoke_hard_quota_concurrent_jobs_429(client):
    """D6/A7: refuse enqueue when queued|running count meets plan.limits.concurrent_jobs."""
    tenant = client.tenant
    plan = Plan.objects.create(code="quota-plan", display_name="Quota", limits={"concurrent_jobs": 1})
    Subscription.objects.create(tenant=tenant, plan=plan, status=Subscription.Status.ACTIVE)
    Job.objects.create(
        tenant=tenant,
        operation="data_science.gnn.embed",
        operation_id=f"op-active-{uuid.uuid4()}",
        status=Job.Status.RUNNING,
    )
    body = {
        "operation": "data_science.gnn.embed",
        "operation_id": f"op-new-{uuid.uuid4()}",
        "input": {"schema_json": "{}", "rows": 0, "payload_digest": "q"},
    }
    resp = client.post(
        "/internal/offload/invoke",
        data=json.dumps(body),
        content_type="application/json",
    )
    assert resp.status_code == 429, resp.content


@pytest.mark.django_db
@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
def test_r_offload_sets_renv_code_ref(client, monkeypatch):
    monkeypatch.setenv("RENV_LOCKFILE_HASH", "renv-hash-fixture")
    op_id = f"op-r-{uuid.uuid4()}"
    body = {
        "operation": "data_science.r.survey_weight",
        "operation_id": op_id,
        "input": {"schema_json": "{}", "rows": 0, "payload_digest": "r1"},
    }
    captured = {}

    class CapturingClient:
        def post_derivation(self, payload):
            captured["payload"] = payload
            return {"ok": True}

    monkeypatch.setattr(
        "bridges.rust_provenance.StubProvenanceClient",
        lambda: CapturingClient(),
    )
    resp = client.post(
        "/internal/offload/invoke",
        data=json.dumps(body),
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    assert captured["payload"]["agent"]["name"] == "R"
    assert captured["payload"]["activity"]["code_ref"] == "renv-hash-fixture"


@pytest.mark.django_db
@override_settings(CELERY_TASK_ALWAYS_EAGER=True)
def test_cancel_terminal_job(client):
    job = Job.objects.create(
        tenant=client.tenant,
        operation="data_science.gnn.denoise",
        operation_id=f"op-{uuid.uuid4()}",
        status=Job.Status.QUEUED,
    )
    # Force non-eager path for cancel itself; status flip is synchronous in view
    resp = client.post(f"/internal/offload/{job.id}/cancel")
    assert resp.status_code == 200
    job.refresh_from_db()
    assert job.status == Job.Status.CANCELED


@pytest.mark.django_db
def test_unknown_operation_rejected(client):
    body = {
        "operation": "not.a.real.op",
        "operation_id": "x",
        "input": {"payload_digest": "z"},
    }
    resp = client.post(
        "/internal/offload/invoke",
        data=json.dumps(body),
        content_type="application/json",
    )
    assert resp.status_code == 400


@pytest.mark.django_db
def test_invoke_requires_a_machine_key():
    body = {
        "operation": "data_science.gnn.embed",
        "operation_id": f"op-unauthenticated-{uuid.uuid4()}",
        "input": {"schema_json": "{}", "rows": 0, "payload_digest": "unauthenticated"},
    }
    response = Client().post(
        "/internal/offload/invoke",
        data=json.dumps(body),
        content_type="application/json",
    )
    assert response.status_code == 401
    assert Job.objects.count() == 0


@pytest.mark.django_db
def test_artifact_upload_capability_is_tenant_scoped(client, monkeypatch):
    class FakeArtifactStore:
        presign_seconds = 300

        def allocate_input_key(self, tenant_id):
            return f"tenants/{tenant_id}/inputs/upload.arrow"

        def presign_put(self, tenant_id, artifact_key):
            assert artifact_key.startswith(f"tenants/{tenant_id}/")
            return "https://storage.example/upload"

    monkeypatch.setattr(
        "apps.orchestration.api.ArtifactStore.from_settings",
        lambda: FakeArtifactStore(),
    )

    response = client.post("/internal/offload/artifact-upload")

    assert response.status_code == 200, response.content
    assert response.json() == {
        "artifact_key": f"tenants/{client.tenant.id}/inputs/upload.arrow",
        "upload_url": "https://storage.example/upload",
        "expires_in_seconds": 300,
        "required_headers": {
            "Content-Type": "application/vnd.apache.arrow.stream",
        },
    }


@pytest.mark.django_db
def test_artifact_upload_returns_503_when_storage_cannot_presign(client, monkeypatch):
    from apps.orchestration.artifacts import ArtifactStorageError

    class FailingArtifactStore:
        def allocate_input_key(self, tenant_id):
            return f"tenants/{tenant_id}/inputs/upload.arrow"

        def presign_put(self, tenant_id, artifact_key):
            raise ArtifactStorageError("artifact storage presign PUT failed")

    monkeypatch.setattr(
        "apps.orchestration.api.ArtifactStore.from_settings",
        lambda: FailingArtifactStore(),
    )

    response = client.post("/internal/offload/artifact-upload")

    assert response.status_code == 503
    assert response.json()["detail"] == "artifact storage unavailable"


@pytest.mark.django_db
@override_settings(OFFLOAD_EXECUTION_MODE="runpod")
def test_live_offload_requires_a_tenant_artifact_key(client, monkeypatch):
    class FakeArtifactStore:
        def validate_key(self, tenant_id, artifact_key):
            if not artifact_key:
                from apps.orchestration.artifacts import ArtifactValidationError

                raise ArtifactValidationError("artifact_key is required")
            return artifact_key

    monkeypatch.setattr(
        "apps.orchestration.api.ArtifactStore.from_settings",
        lambda: FakeArtifactStore(),
    )
    body = {
        "operation": "data_science.community.assign",
        "operation_id": f"op-artifact-{uuid.uuid4()}",
        "input": {"schema_json": "{}", "rows": 0, "payload_digest": "sha256:" + "0" * 64},
    }

    response = client.post("/internal/offload/invoke", data=json.dumps(body), content_type="application/json")

    assert response.status_code == 400
    assert "artifact_key is required" in response.json()["detail"]


@pytest.mark.django_db
@override_settings(OFFLOAD_EXECUTION_MODE="runpod")
def test_live_offload_requires_a_complete_arrow_descriptor(client, monkeypatch):
    class FakeArtifactStore:
        def validate_key(self, tenant_id, artifact_key):
            return artifact_key

    monkeypatch.setattr(
        "apps.orchestration.api.ArtifactStore.from_settings",
        lambda: FakeArtifactStore(),
    )
    body = {
        "operation": "data_science.community.assign",
        "operation_id": f"op-incomplete-{uuid.uuid4()}",
        "input": {
            "artifact_key": f"tenants/{client.tenant.id}/inputs/input.arrow",
            "schema_json": "",
            "rows": None,
            "payload_digest": "not-a-digest",
        },
    }

    response = client.post("/internal/offload/invoke", data=json.dumps(body), content_type="application/json")

    assert response.status_code == 400
    assert "sha256 digest" in response.json()["detail"]


@pytest.mark.django_db
def test_invoke_rejects_key_without_required_scope():
    tenant = Tenant.objects.create(slug=f"scope-{uuid.uuid4()}", display_name="Scope")
    minted = mint_api_key(tenant, scopes=["offload:read"])
    body = {
        "operation": "data_science.gnn.embed",
        "operation_id": f"op-scope-{uuid.uuid4()}",
        "input": {"schema_json": "{}", "rows": 0, "payload_digest": "scope"},
    }
    response = Client().post(
        "/internal/offload/invoke",
        data=json.dumps(body),
        content_type="application/json",
        headers={"Authorization": f"Bearer {minted.plaintext}"},
    )
    assert response.status_code == 403
    assert Job.objects.count() == 0


@pytest.mark.django_db
def test_job_read_does_not_cross_tenant_boundary(client):
    other = Tenant.objects.create(slug=f"other-{uuid.uuid4()}", display_name="Other")
    job = Job.objects.create(
        tenant=other,
        operation="data_science.gnn.embed",
        operation_id=f"op-other-{uuid.uuid4()}",
        status=Job.Status.QUEUED,
    )
    response = client.get(f"/internal/offload/{job.id}")
    assert response.status_code == 404
