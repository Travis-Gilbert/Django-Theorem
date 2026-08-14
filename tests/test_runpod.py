"""RunPod Serverless protocol and Django task integration tests."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from django.test import override_settings

from apps.orchestration.models import Job
from apps.orchestration.runpod import RunpodServerlessClient
from apps.orchestration.tasks import run_offload_python, run_offload_r


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
    class FakeRunpodClient:
        SUCCESS = "COMPLETED"

        def __init__(self, **kwargs):
            assert kwargs["endpoint_id"] == "endpoint-1"

        def submit(self, payload):
            assert payload["contract"] == "theorem.offload.v1"
            assert payload["input"]["payload_digest"] == "input-1"
            return type("Submitted", (), {"job_id": "remote-1", "status": "IN_QUEUE"})()

        def wait(self, job_id, **kwargs):
            assert job_id == "remote-1"
            kwargs["on_update"]({"status": "IN_PROGRESS"})
            return {
                "status": "COMPLETED",
                "output": {"schema_json": "{}", "rows": 2, "payload_digest": "output-1"},
            }

    monkeypatch.setattr("apps.orchestration.tasks.RunpodServerlessClient", FakeRunpodClient)
    monkeypatch.setattr(
        "bridges.rust_provenance.StubProvenanceClient.post_derivation",
        lambda self, payload: {"ok": True},
    )
    job = Job.objects.create(
        operation="data_science.gnn.embed",
        operation_id=f"runpod-{uuid.uuid4()}",
        input_payload_digest="input-1",
        kwargs_json={"input_schema_json": "{}", "input_rows": 1, "params": {"dim": 8}},
        status=Job.Status.RUNNING,
    )

    result = run_offload_python(str(job.id))
    job.refresh_from_db()
    assert result["status"] == "succeeded"
    assert job.status == Job.Status.SUCCEEDED
    assert job.output_payload_digest == "output-1"
    assert job.kwargs_json["runpod"]["job_id"] == "remote-1"
    assert "IN_PROGRESS" in job.logs


@pytest.mark.django_db
@override_settings(R_OFFLOAD_EXECUTION_MODE="rpy2")
def test_r_task_refuses_synthetic_success_until_artifact_handoff_exists(monkeypatch):
    monkeypatch.delenv("RENV_LOCKFILE_HASH", raising=False)
    monkeypatch.setattr(
        "apps.orchestration.r_runtime.runtime_identity",
        lambda: "R version 4.4.3; rpy2 3.6.0",
    )
    monkeypatch.setattr(
        "apps.orchestration.r_runtime.renv_lockfile_hash",
        lambda: "renv-lock-fixture",
    )
    job = Job.objects.create(
        operation="data_science.r.survey_weight",
        operation_id=f"r-{uuid.uuid4()}",
        status=Job.Status.RUNNING,
    )

    result = run_offload_r(str(job.id))
    job.refresh_from_db()
    assert result["status"] == "failed"
    assert job.status == Job.Status.FAILED
    assert "R2 Arrow artifact handoff" in job.error
