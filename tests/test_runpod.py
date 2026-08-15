"""RunPod Serverless protocol and Django task integration tests."""

from __future__ import annotations

import json
import sys
import uuid
from types import SimpleNamespace

import httpx
import pyarrow as pa
import pytest
from django.test import override_settings

from apps.orchestration.artifacts import StoredArrowArtifact, arrow_schema_json, encode_arrow_ipc, sha256_digest
from apps.orchestration.models import Job
from apps.orchestration.runpod import RunpodServerlessClient
from apps.orchestration.tasks import run_offload_python, run_offload_r
from apps.tenancy.models import Tenant


def test_serverless_client_submits_polls_and_cancels_with_bearer_auth():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v2/endpoint-1/run":
            assert json.loads(request.content) == {"input": {"contract": "theorem.offload.v1"}}
            return httpx.Response(200, json={"id": "remote-1", "status": "IN_QUEUE"})
        if request.url.path == "/v2/endpoint-1/status/remote-1":
            return httpx.Response(
                200,
                json={
                    "id": "remote-1",
                    "status": "COMPLETED",
                    "output": {"schema_json": "{}", "rows": 1, "payload_digest": "out-1"},
                },
            )
        if request.url.path == "/v2/endpoint-1/cancel/remote-1":
            return httpx.Response(200, json={"id": "remote-1", "status": "CANCELLED"})
        return httpx.Response(404, json={"error": "unexpected request"})

    client = RunpodServerlessClient(
        api_key="test-key",
        endpoint_id="endpoint-1",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    submitted = client.submit({"contract": "theorem.offload.v1"})
    assert submitted.job_id == "remote-1"
    completed = client.wait(
        submitted.job_id,
        timeout_seconds=1,
        poll_interval_seconds=0.1,
    )
    assert completed["status"] == "COMPLETED"
    assert client.cancel(submitted.job_id)["status"] == "CANCELLED"
    assert all(request.headers["Authorization"] == "Bearer test-key" for request in requests)


@pytest.mark.django_db
@override_settings(
    OFFLOAD_EXECUTION_MODE="runpod",
    RUNPOD_API_KEY="test-key",
    RUNPOD_SERVERLESS_ENDPOINT_ID="endpoint-1",
    RUNPOD_WORKER_IMAGE_DIGEST="registry.example/theorem@sha256:immutable",
    RUNPOD_JOB_TIMEOUT_SECONDS=1,
    RUNPOD_POLL_INTERVAL_SECONDS=0.1,
)
def test_runpod_task_persists_remote_job_and_requires_arrow_descriptor(monkeypatch):
    output_table = pa.table({"node": pa.array(["a", "b"]), "community_id": pa.array([0, 0])})
    output_payload = encode_arrow_ipc(output_table)
    output_digest = sha256_digest(output_payload)
    output_schema = arrow_schema_json(output_table.schema)

    class FakeArtifactStore:
        def output_key(self, tenant_id, operation_id):
            return f"tenants/{tenant_id}/outputs/{operation_id}.arrow"

        def validate_key(self, tenant_id, artifact_key):
            assert artifact_key.startswith(f"tenants/{tenant_id}/")
            return artifact_key

        def presign_get(self, tenant_id, artifact_key):
            self.validate_key(tenant_id, artifact_key)
            return "https://storage.example/input"

        def presign_put(self, tenant_id, artifact_key):
            self.validate_key(tenant_id, artifact_key)
            return "https://storage.example/output"

        def read_arrow(self, tenant_id, artifact_key, **expected):
            self.validate_key(tenant_id, artifact_key)
            assert expected == {
                "expected_digest": output_digest,
                "expected_schema_json": output_schema,
                "expected_rows": output_table.num_rows,
            }
            return StoredArrowArtifact(
                artifact_key=artifact_key,
                payload_digest=output_digest,
                schema_json=output_schema,
                rows=output_table.num_rows,
            )

    class FakeRunpodClient:
        SUCCESS = "COMPLETED"

        def __init__(self, **kwargs):
            assert kwargs["endpoint_id"] == "endpoint-1"

        def submit(self, payload):
            assert payload["contract"] == "theorem.offload.v1"
            assert payload["input"]["payload_digest"].startswith("sha256:")
            assert payload["input"]["read_url"] == "https://storage.example/input"
            assert payload["output"]["write_url"] == "https://storage.example/output"
            return type("Submitted", (), {"job_id": "remote-1", "status": "IN_QUEUE"})()

        def wait(self, job_id, **kwargs):
            assert job_id == "remote-1"
            kwargs["on_update"]({"status": "IN_PROGRESS"})
            return {
                "status": "COMPLETED",
                "output": {
                    "output": {
                        "schema_json": output_schema,
                        "rows": output_table.num_rows,
                        "payload_digest": output_digest,
                    }
                },
            }

    monkeypatch.setattr("apps.orchestration.tasks.RunpodServerlessClient", FakeRunpodClient)
    monkeypatch.setattr("apps.orchestration.tasks.ArtifactStore.from_settings", lambda: FakeArtifactStore())
    monkeypatch.setattr(
        "bridges.rust_provenance.StubProvenanceClient.post_derivation",
        lambda self, payload: {"ok": True},
    )
    tenant = Tenant.objects.create(slug=f"runpod-{uuid.uuid4()}", display_name="RunPod")
    job = Job.objects.create(
        tenant=tenant,
        operation="data_science.gnn.embed",
        operation_id=f"runpod-{uuid.uuid4()}",
        input_payload_digest=sha256_digest(b"input"),
        kwargs_json={
            "input_schema_json": "{}",
            "input_rows": 1,
            "input_artifact_key": f"tenants/{tenant.id}/inputs/input.arrow",
            "params": {"dim": 8},
        },
        status=Job.Status.RUNNING,
    )

    result = run_offload_python(str(job.id))
    job.refresh_from_db()
    assert result["status"] == "succeeded"
    assert job.status == Job.Status.SUCCEEDED
    assert job.output_payload_digest == output_digest
    assert job.kwargs_json["runpod"]["job_id"] == "remote-1"
    assert "IN_PROGRESS" in job.logs


@pytest.mark.django_db
@override_settings(
    OFFLOAD_EXECUTION_MODE="runpod",
    RUNPOD_API_KEY="test-key",
    RUNPOD_SERVERLESS_ENDPOINT_ID="endpoint-1",
    RUNPOD_WORKER_IMAGE_DIGEST="registry.example/theorem@sha256:immutable",
)
def test_runpod_task_fails_when_output_readback_hits_storage_error(monkeypatch):
    from apps.orchestration.artifacts import ArtifactStorageError

    tenant = Tenant.objects.create(slug=f"runpod-storage-{uuid.uuid4()}", display_name="RunPod")
    job = Job.objects.create(
        tenant=tenant,
        operation="data_science.community.assign",
        operation_id=f"runpod-storage-{uuid.uuid4()}",
        input_payload_digest="sha256:" + "0" * 64,
        kwargs_json={
            "input_schema_json": "{}",
            "input_rows": 1,
            "input_artifact_key": f"tenants/{tenant.id}/inputs/input.arrow",
        },
        status=Job.Status.RUNNING,
    )

    class FailingArtifactStore:
        def output_key(self, tenant_id, operation_id):
            return f"tenants/{tenant_id}/outputs/{operation_id}.arrow"

        def validate_key(self, tenant_id, artifact_key):
            return artifact_key

        def presign_get(self, tenant_id, artifact_key):
            return "https://storage.example/input"

        def presign_put(self, tenant_id, artifact_key):
            return "https://storage.example/output"

        def read_arrow(self, tenant_id, artifact_key, **expected):
            raise ArtifactStorageError("artifact storage read failed")

    class CompletedRunpodClient:
        SUCCESS = "COMPLETED"

        def __init__(self, **kwargs):
            pass

        def submit(self, payload):
            return type("Submitted", (), {"job_id": "remote-1", "status": "IN_QUEUE"})()

        def wait(self, job_id, **kwargs):
            return {
                "status": "COMPLETED",
                "output": {
                    "output": {
                        "schema_json": "{}",
                        "rows": 1,
                        "payload_digest": "sha256:" + "1" * 64,
                    }
                },
            }

    monkeypatch.setattr("apps.orchestration.tasks.ArtifactStore.from_settings", lambda: FailingArtifactStore())
    monkeypatch.setattr("apps.orchestration.tasks.RunpodServerlessClient", CompletedRunpodClient)

    result = run_offload_python(str(job.id))

    job.refresh_from_db()
    assert result["status"] == "failed"
    assert job.status == Job.Status.FAILED
    assert "artifact storage read failed" in job.error


@pytest.mark.django_db
@override_settings(R_OFFLOAD_EXECUTION_MODE="rpy2")
def test_r_task_runs_real_weighted_mean_from_arrow_artifact(monkeypatch):
    monkeypatch.delenv("RENV_LOCKFILE_HASH", raising=False)
    monkeypatch.setattr(
        "apps.orchestration.r_runtime.runtime_identity",
        lambda: "R version 4.4.3; rpy2 3.6.0",
    )
    monkeypatch.setattr(
        "apps.orchestration.r_runtime.renv_lockfile_hash",
        lambda: "renv-lock-fixture",
    )
    input_table = pa.table({"value": [10.0, 20.0, 30.0], "weight": [1.0, 2.0, 1.0]})
    input_payload = encode_arrow_ipc(input_table)
    tenant = Tenant.objects.create(slug=f"r-{uuid.uuid4()}", display_name="R")

    class FakeArtifactStore:
        def read_table(self, tenant_id, artifact_key, **expected):
            assert tenant_id == tenant.id
            assert artifact_key.startswith(f"tenants/{tenant.id}/")
            assert expected["expected_digest"] == sha256_digest(input_payload)
            return input_table

        def output_key(self, tenant_id, operation_id):
            return f"tenants/{tenant_id}/outputs/{operation_id}.arrow"

        def write_table(self, tenant_id, artifact_key, table):
            assert tenant_id == tenant.id
            payload = encode_arrow_ipc(table)
            return StoredArrowArtifact(
                artifact_key=artifact_key,
                payload_digest=sha256_digest(payload),
                schema_json=arrow_schema_json(table.schema),
                rows=table.num_rows,
            )

    fake_robjects = SimpleNamespace(
        FloatVector=lambda values: list(values),
        r={"weighted.mean": lambda values, weights: [sum(v * w for v, w in zip(values, weights, strict=True)) / sum(weights)]},
    )
    monkeypatch.setattr("apps.orchestration.tasks.ArtifactStore.from_settings", lambda: FakeArtifactStore())
    monkeypatch.setitem(sys.modules, "rpy2", SimpleNamespace(robjects=fake_robjects))
    job = Job.objects.create(
        tenant=tenant,
        operation="data_science.r.survey_weight",
        operation_id=f"r-{uuid.uuid4()}",
        input_payload_digest=sha256_digest(input_payload),
        kwargs_json={
            "input_artifact_key": f"tenants/{tenant.id}/inputs/survey.arrow",
            "input_schema_json": arrow_schema_json(input_table.schema),
            "input_rows": input_table.num_rows,
        },
        status=Job.Status.RUNNING,
    )

    result = run_offload_r(str(job.id))
    job.refresh_from_db()
    assert result["status"] == "succeeded"
    assert job.status == Job.Status.SUCCEEDED
    assert job.output_rows == 1
    assert "weighted_mean" in job.output_schema_json
