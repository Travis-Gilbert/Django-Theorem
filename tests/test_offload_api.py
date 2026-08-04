"""A8 — offload invoke / status / cancel + operation_id idempotency."""

from __future__ import annotations

import json
import uuid

import pytest
from django.test import Client, override_settings

from apps.orchestration.models import Job


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
