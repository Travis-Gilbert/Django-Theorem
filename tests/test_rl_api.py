"""Tenant, scope, idempotency, and descriptor oracles for /internal/rl."""

from __future__ import annotations

import json
from io import BytesIO
from uuid import uuid4

import pytest
from django.test import Client

from apps.keys.mint import mint_api_key
from apps.orchestration.artifacts import ArtifactStore
from apps.tenancy.models import Tenant
from theorem_control.rl.ingest import ingest_trajectories
from theorem_control.rl.models import TrainingRun


class MemoryS3Client:
    def __init__(self):
        self.objects = {}

    def put_object(self, *, Bucket, Key, Body, ContentType):
        self.objects[(Bucket, Key)] = bytes(Body)
        return {}

    def get_object(self, *, Bucket, Key):
        payload = self.objects[(Bucket, Key)]
        return {"ContentLength": len(payload), "Body": BytesIO(payload)}

    def delete_object(self, *, Bucket, Key):
        self.objects.pop((Bucket, Key), None)
        return {}

    def generate_presigned_url(self, _action, *, Params, ExpiresIn, HttpMethod):
        return f"https://storage.example/{Params['Key']}?expires={ExpiresIn}&method={HttpMethod}"


@pytest.fixture
def artifact_store():
    return ArtifactStore(
        endpoint_url="https://storage.example",
        access_key_id="access",
        secret_access_key="secret",
        region="us-east-2",
        bucket="rl",
        presign_seconds=300,
        max_bytes=1024 * 1024,
        client=MemoryS3Client(),
    )


@pytest.fixture
def rl_client(db, monkeypatch):
    tenant = Tenant.objects.create(slug=f"rl-{uuid4()}", display_name="RL tenant")
    key = mint_api_key(tenant, scopes=["rl:*"])
    client = Client(HTTP_AUTHORIZATION=f"Bearer {key.plaintext}")
    client.tenant = tenant
    monkeypatch.setattr("theorem_control.rl.api.run_eval.delay", lambda _run_id: None)
    monkeypatch.setattr(
        "theorem_control.rl.api.run_prime_rl_training.delay", lambda _run_id: None
    )
    return client


def post_json(client, path, value):
    return client.post(path, data=json.dumps(value), content_type="application/json")


@pytest.mark.django_db
def test_eval_is_tenant_bound_idempotent_and_conflicts_fail_closed(rl_client):
    body = {
        "operation_id": "django-v1-eval-1",
        "taskset_ref": "django-v1",
        "config": {"model": "fixture-model", "num_tasks": 5, "num_rollouts": 3},
    }
    first = post_json(rl_client, "/internal/rl/eval", body)
    second = post_json(rl_client, "/internal/rl/eval", body)
    conflict_body = {**body, "config": {**body["config"], "num_tasks": 6}}
    conflict = post_json(rl_client, "/internal/rl/eval", conflict_body)

    assert first.status_code == 202, first.content
    assert second.status_code == 202, second.content
    assert first.json()["reused"] is False
    assert second.json()["reused"] is True
    assert second.json()["run_id"] == first.json()["run_id"]
    assert conflict.status_code == 409
    assert TrainingRun.objects.count() == 1


@pytest.mark.django_db
def test_scope_and_tenant_substitution_are_refused(rl_client):
    weak = mint_api_key(rl_client.tenant, scopes=["rl:read"])
    weak_client = Client(HTTP_AUTHORIZATION=f"Bearer {weak.plaintext}")
    body = {
        "operation_id": "django-v1-eval-scope",
        "taskset_ref": "django-v1",
        "config": {},
    }
    assert post_json(Client(), "/internal/rl/eval", body).status_code == 401
    assert post_json(weak_client, "/internal/rl/eval", body).status_code == 403

    substituted = {
        **body,
        "operation_id": "django-v1-eval-substitution",
        "config": {"tenant_id": str(uuid4())},
    }
    response = post_json(rl_client, "/internal/rl/eval", substituted)
    assert response.status_code == 403
    assert TrainingRun.objects.count() == 0


@pytest.mark.django_db
def test_training_requires_an_immutable_image_digest(rl_client):
    for operation_id, image in (
        ("django-v1-train-tag", "ghcr.io/example/django-v1:latest"),
        ("django-v1-train-short", "ghcr.io/example/django-v1@sha256:abc"),
        ("django-v1-train-empty", "@sha256:" + "a" * 64),
    ):
        response = post_json(
            rl_client,
            "/internal/rl/train",
            {
                "operation_id": operation_id,
                "taskset_ref": "django-v1",
                "config": {},
                "image_digest": image,
            },
        )
        assert response.status_code == 400
    assert TrainingRun.objects.count() == 0


@pytest.mark.django_db
def test_status_verifies_arrow_descriptor_and_is_tenant_isolated(
    rl_client,
    artifact_store,
    monkeypatch,
):
    run = TrainingRun.objects.create(
        tenant=rl_client.tenant,
        operation=TrainingRun.Operation.EVAL,
        operation_id="django-v1-eval-status",
        taskset_ref="django-v1",
        config_digest="sha256:" + "a" * 64,
        config_json={},
        status=TrainingRun.Status.SUCCEEDED,
    )
    traces = (
        json.dumps(
            {
                "task_key": "repair-model-01",
                "trace": {"reply": "ok"},
                "reward": 1.0,
                "metrics": {"resolved": 1.0},
            }
        )
        + "\n"
    ).encode()
    descriptor = ingest_trajectories(run, traces, store=artifact_store)
    monkeypatch.setattr(
        "theorem_control.rl.api.ArtifactStore.from_settings", lambda: artifact_store
    )

    response = rl_client.get(f"/internal/rl/runs/{run.id}")
    assert response.status_code == 200, response.content
    assert response.json()["trajectory_count"] == 1
    assert (
        response.json()["artifacts"][0]["payload_digest"]
        == descriptor["payload_digest"]
    )
    assert response.json()["artifacts"][0]["download_url"].startswith(
        "https://storage.example/"
    )

    other = Tenant.objects.create(slug=f"other-{uuid4()}", display_name="Other")
    other_key = mint_api_key(other, scopes=["rl:read"])
    hidden = Client(HTTP_AUTHORIZATION=f"Bearer {other_key.plaintext}").get(
        f"/internal/rl/runs/{run.id}"
    )
    assert hidden.status_code == 404


@pytest.mark.django_db
def test_cancel_is_idempotent(rl_client):
    run = TrainingRun.objects.create(
        tenant=rl_client.tenant,
        operation=TrainingRun.Operation.EVAL,
        operation_id="django-v1-eval-cancel",
        taskset_ref="django-v1",
        config_digest="sha256:" + "b" * 64,
        config_json={},
    )
    first = rl_client.post(f"/internal/rl/runs/{run.id}/cancel")
    second = rl_client.post(f"/internal/rl/runs/{run.id}/cancel")
    assert first.status_code == 200
    assert first.json()["status"] == "canceled"
    assert first.json()["reused"] is False
    assert second.json()["reused"] is True
