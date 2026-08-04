"""A8 — offload invoke / status / cancel + operation_id idempotency + D6 quota."""

from __future__ import annotations

import json
import uuid

import pytest
from django.test import Client, override_settings

from apps.billing.models import Plan, Subscription
from apps.orchestration.models import Job
from apps.tenancy.models import Tenant


@pytest.fixture
def client():
    return Client()


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
def test_invoke_concurrent_idempotency_while_queued(client):
    """A8-DJ: same operation_id while first still queued returns same job_id (DB unique)."""
    op_id = f"op-queued-{uuid.uuid4()}"
    existing = Job.objects.create(
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
    tenant = Tenant.objects.create(slug="quota-tenant", display_name="Quota")
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
        "tenant_id": str(tenant.id),
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
        operation="data_science.gnn.denoise",
        operation_id=f"op-{uuid.uuid4()}",
        status=Job.Status.QUEUED,
    )
    # Force non-eager path for cancel itself; status flip is synchronous in view
    resp = client.post(f"/internal/offload/{job.id}/cancel")
    assert resp.status_code == 200
    job.refresh_from_db()
    assert job.status == Job.Status.CANCELED


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
