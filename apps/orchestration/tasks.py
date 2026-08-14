"""Celery offload tasks — routed by operation name; idempotent on operation_id.

R queue
-------
Tasks whose operation starts with ``data_science.r.`` are queued onto
``offload.r``. The base control-plane worker dispatches GPU/Python operations to
RunPod Serverless; the dedicated R worker keeps the R runtime separate from the
web and light-worker image.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from collections.abc import Mapping
from typing import Any

from celery import shared_task
from django.conf import settings
from django.utils import timezone as dj_timezone
import pyarrow as pa

from apps.orchestration.artifacts import (
    ArtifactConfigurationError,
    ArtifactStore,
    ArtifactStorageError,
    ArtifactValidationError,
    is_sha256_digest,
)
from apps.orchestration.runpod import (
    RunpodApiError,
    RunpodConfigurationError,
    RunpodServerlessClient,
    RunpodTimeoutError,
)

logger = logging.getLogger(__name__)

TASK_VERSION = "0.2.0"
STUB_TASK_VERSION = "0.1.0-stub"
RENV_LOCKFILE_STUB = "renv.lock:stub"


def _is_r_operation(operation: str) -> bool:
    return operation.startswith("data_science.r.")


def renv_code_ref() -> str:
    """Provenance code_ref for R tasks: the active immutable renv lockfile."""
    env_hash = os.environ.get("RENV_LOCKFILE_HASH", "").strip()
    if env_hash:
        return env_hash
    if settings.R_OFFLOAD_EXECUTION_MODE == "rpy2":
        from apps.orchestration.r_runtime import renv_lockfile_hash

        return renv_lockfile_hash()
    return hashlib.sha256(RENV_LOCKFILE_STUB.encode("utf-8")).hexdigest()


@shared_task(name="apps.orchestration.tasks.dispatch_offload", bind=True)
def dispatch_offload(self, job_id: str) -> dict:
    """Route by operation name to the matching stub executor."""
    from apps.orchestration.models import Job

    try:
        job = Job.objects.get(id=job_id)
    except Job.DoesNotExist:
        logger.error("dispatch_offload: job %s missing", job_id)
        return {"error": "missing"}

    if job.status == Job.Status.CANCELED:
        return {"status": "canceled"}

    # Idempotency: another worker may already own a finished/running twin.
    twin = (
        Job.objects.filter(tenant_id=job.tenant_id, operation_id=job.operation_id)
        .exclude(id=job.id)
        .filter(status__in=[Job.Status.RUNNING, Job.Status.SUCCEEDED])
        .first()
    )
    if twin is not None:
        logger.info("joining existing job %s for operation_id %s", twin.id, job.operation_id)
        return {"status": twin.status, "joined": str(twin.id)}

    job.status = Job.Status.RUNNING
    job.started_at = dj_timezone.now()
    job.celery_task_id = self.request.id or job.celery_task_id
    job.save(update_fields=["status", "started_at", "celery_task_id", "updated_at"])

    if _is_r_operation(job.operation):
        # Calling the task directly here would execute it on the default worker
        # and silently bypass the dedicated R image. apply_async preserves the
        # queue boundary even when CELERY_TASK_ALWAYS_EAGER is used in tests.
        result = run_offload_r.apply_async(args=[str(job.id)], queue=settings.CELERY_R_QUEUE)
        job.celery_task_id = result.id or job.celery_task_id
        job.save(update_fields=["celery_task_id", "updated_at"])
        return {"status": "queued", "celery_task_id": job.celery_task_id}
    return run_offload_python(str(job.id))


@shared_task(name="apps.orchestration.tasks.run_offload_python")
def run_offload_python(job_id: str) -> dict:
    """Execute Python/GPU offload through the configured RunPod Serverless endpoint."""
    if settings.OFFLOAD_EXECUTION_MODE == "runpod":
        return _run_runpod_offload(job_id)
    if settings.OFFLOAD_EXECUTION_MODE != "stub":
        return _fail_job(
            job_id,
            f"unsupported OFFLOAD_EXECUTION_MODE={settings.OFFLOAD_EXECUTION_MODE!r}",
            log_prefix="RunPod",
        )
    # Explicit deterministic stand-in for local/tests only. It is never a live
    # data-science oracle.
    return _complete_stub(
        job_id,
        engine="celery",
        agent_name="theorem-control-plane",
        code_ref="stub-local",
    )


@shared_task(name="apps.orchestration.tasks.run_offload_r", queue="offload.r")
def run_offload_r(job_id: str) -> dict:
    """Run on the dedicated R queue; production boot verifies R through rpy2."""
    if settings.R_OFFLOAD_EXECUTION_MODE == "rpy2":
        return _run_r_offload(job_id)
    if settings.R_OFFLOAD_EXECUTION_MODE != "stub":
        return _fail_job(
            job_id,
            f"unsupported R_OFFLOAD_EXECUTION_MODE={settings.R_OFFLOAD_EXECUTION_MODE!r}",
            log_prefix="R",
        )
    # Explicit deterministic stand-in for local/tests only.
    return _complete_stub(
        job_id,
        engine="celery",
        agent_name="R",
        code_ref=renv_code_ref(),
    )


def _complete_stub(job_id: str, *, engine: str, agent_name: str, code_ref: str) -> dict:
    from apps.orchestration.models import Job
    from bridges.rust_provenance import StubProvenanceClient

    job = Job.objects.get(id=job_id)
    if job.status == Job.Status.CANCELED:
        return {"status": "canceled"}

    started_ms = int((job.started_at or dj_timezone.now()).timestamp() * 1000)
    # Synthetic output — live RunPod/rpy2 replaces this body.
    out_digest = hashlib.sha256(
        f"{job.operation_id}:{job.input_payload_digest}:stub".encode()
    ).hexdigest()
    job.output_payload_digest = out_digest
    job.output_schema_json = job.kwargs_json.get("input_schema_json", "{}")
    job.output_rows = job.kwargs_json.get("input_rows")
    job.logs = f"[StubOffload] completed {job.operation} via {agent_name}\n"
    job.status = Job.Status.SUCCEEDED
    job.ended_at = dj_timezone.now()
    job.save()

    ended_ms = int(job.ended_at.timestamp() * 1000)
    params_hash = hashlib.sha256(
        json.dumps(job.kwargs_json.get("params") or {}, sort_keys=True).encode()
    ).hexdigest()

    client = StubProvenanceClient()
    client.post_derivation(
        {
            "activity": {
                "name": job.operation,
                "engine": engine,
                "engine_version": STUB_TASK_VERSION,
                "params_hash": params_hash,
                "code_ref": code_ref,
                "started_at_ms": started_ms,
                "ended_at_ms": ended_ms,
            },
            "agent": {"kind": "tool", "name": agent_name, "version": STUB_TASK_VERSION},
            "inputs": job.kwargs_json.get("input_entity_ids") or [],
            "outputs": [
                {
                    "kind": "artifact",
                    "name": f"{job.operation}:output",
                    "content_hash": out_digest,
                }
            ],
        }
    )
    return {
        "status": "succeeded",
        "output_payload_digest": out_digest,
        "code_ref": code_ref,
        "agent_name": agent_name,
    }


def _runpod_input(job, *, store: ArtifactStore, output_artifact_key: str) -> dict[str, Any]:
    """The versioned, byte-free input contract accepted by Theorem RunPod workers."""
    if job.tenant_id is None:
        raise ArtifactValidationError("RunPod jobs require an admitted tenant")
    input_artifact_key = str(job.kwargs_json.get("input_artifact_key") or "")
    store.validate_key(job.tenant_id, input_artifact_key)
    store.validate_key(job.tenant_id, output_artifact_key)
    if not is_sha256_digest(job.input_payload_digest):
        raise ArtifactValidationError("RunPod input payload digest must be a sha256 digest")
    if not job.kwargs_json.get("input_schema_json") or job.kwargs_json.get("input_rows") is None:
        raise ArtifactValidationError("RunPod input schema and row count are required")
    return {
        "contract": "theorem.offload.v1",
        "operation": job.operation,
        "operation_id": job.operation_id,
        "input": {
            "schema_json": job.kwargs_json.get("input_schema_json", ""),
            "rows": job.kwargs_json.get("input_rows"),
            "payload_digest": job.input_payload_digest,
            "artifact_key": input_artifact_key,
            "read_url": store.presign_get(job.tenant_id, input_artifact_key),
        },
        "output": {
            "artifact_key": output_artifact_key,
            "write_url": store.presign_put(job.tenant_id, output_artifact_key),
        },
        "params": job.kwargs_json.get("params") or {},
    }


def _append_log(job, line: str) -> None:
    # Status/log persistence is bounded so a verbose remote worker cannot turn a
    # control-plane record into an unbounded payload store.
    job.logs = f"{job.logs}{line.rstrip()}\n"[-16_000:]


def _fail_job(job_id: str, error: str, *, log_prefix: str) -> dict[str, str]:
    from apps.orchestration.models import Job

    job = Job.objects.get(id=job_id)
    if job.status == Job.Status.CANCELED:
        return {"status": "canceled"}
    job.status = Job.Status.FAILED
    job.error = error
    job.ended_at = dj_timezone.now()
    _append_log(job, f"[{log_prefix}] failed: {error}")
    job.save(update_fields=["status", "error", "ended_at", "logs", "updated_at"])
    return {"status": "failed", "error": error}


def _output_descriptor(output: Any) -> tuple[str, int | None, str]:
    """Validate the worker's Arrow descriptor instead of accepting a fake result."""
    if not isinstance(output, Mapping):
        raise ValueError("RunPod output must be an ArrowBatch descriptor object")
    schema_json = output.get("schema_json")
    payload_digest = output.get("payload_digest")
    rows = output.get("rows")
    if not isinstance(schema_json, str):
        raise ValueError("RunPod output.schema_json must be a string")
    if not isinstance(payload_digest, str) or not is_sha256_digest(payload_digest):
        raise ValueError("RunPod output.payload_digest must be a sha256 digest")
    if rows is not None and (not isinstance(rows, int) or isinstance(rows, bool) or rows < 0):
        raise ValueError("RunPod output.rows must be a non-negative integer or null")
    return schema_json, rows, payload_digest


def _post_provenance(job, *, engine: str, agent_name: str, code_ref: str) -> None:
    from bridges.rust_provenance import StubProvenanceClient

    started_ms = int((job.started_at or dj_timezone.now()).timestamp() * 1000)
    ended_ms = int((job.ended_at or dj_timezone.now()).timestamp() * 1000)
    params_hash = hashlib.sha256(
        json.dumps(job.kwargs_json.get("params") or {}, sort_keys=True).encode()
    ).hexdigest()
    StubProvenanceClient().post_derivation(
        {
            "activity": {
                "name": job.operation,
                "engine": engine,
                "engine_version": TASK_VERSION,
                "params_hash": params_hash,
                "code_ref": code_ref,
                "started_at_ms": started_ms,
                "ended_at_ms": ended_ms,
            },
            "agent": {"kind": "tool", "name": agent_name, "version": TASK_VERSION},
            "inputs": job.kwargs_json.get("input_entity_ids") or [],
            "outputs": [
                {
                    "kind": "artifact",
                    "name": f"{job.operation}:output",
                    "content_hash": job.output_payload_digest,
                }
            ],
        }
    )


def _run_runpod_offload(job_id: str) -> dict[str, str]:
    from apps.orchestration.models import Job

    job = Job.objects.get(id=job_id)
    if job.status == Job.Status.CANCELED:
        return {"status": "canceled"}
    try:
        if job.tenant_id is None:
            raise ArtifactValidationError("RunPod jobs require an admitted tenant")
        store = ArtifactStore.from_settings()
        output_artifact_key = store.output_key(job.tenant_id, job.operation_id)
        client = RunpodServerlessClient(
            api_key=settings.RUNPOD_API_KEY,
            endpoint_id=settings.RUNPOD_SERVERLESS_ENDPOINT_ID,
            base_url=settings.RUNPOD_API_BASE,
            timeout_seconds=settings.RUNPOD_REQUEST_TIMEOUT_SECONDS,
        )
        if not settings.RUNPOD_WORKER_IMAGE_DIGEST:
            raise RunpodConfigurationError("RUNPOD_WORKER_IMAGE_DIGEST is required for provenance")
        submitted = client.submit(
            _runpod_input(job, store=store, output_artifact_key=output_artifact_key)
        )
        metadata = dict(job.kwargs_json)
        metadata["output_artifact_key"] = output_artifact_key
        metadata["runpod"] = {
            "endpoint_id": settings.RUNPOD_SERVERLESS_ENDPOINT_ID,
            "job_id": submitted.job_id,
        }
        job.kwargs_json = metadata
        _append_log(job, f"[RunPod] submitted {submitted.job_id}: {submitted.status}")
        job.save(update_fields=["kwargs_json", "logs", "updated_at"])

        last_status = ""
        last_logs_hash = ""

        def persist_status(state: Mapping[str, Any]) -> None:
            nonlocal last_logs_hash, last_status
            status = str(state["status"])
            changed = False
            if status != last_status:
                _append_log(job, f"[RunPod] {submitted.job_id}: {status}")
                last_status = status
                changed = True
            remote_logs = state.get("logs")
            if remote_logs:
                rendered_logs = (
                    remote_logs
                    if isinstance(remote_logs, str)
                    else json.dumps(remote_logs, sort_keys=True)
                )
                logs_hash = hashlib.sha256(rendered_logs.encode()).hexdigest()
                if logs_hash != last_logs_hash:
                    _append_log(job, f"[RunPod remote] {rendered_logs[-2_000:]}")
                    last_logs_hash = logs_hash
                    changed = True
            if changed:
                job.save(update_fields=["logs", "updated_at"])

        result = client.wait(
            submitted.job_id,
            timeout_seconds=settings.RUNPOD_JOB_TIMEOUT_SECONDS,
            poll_interval_seconds=settings.RUNPOD_POLL_INTERVAL_SECONDS,
            on_update=persist_status,
        )
    except (
        ArtifactConfigurationError,
        ArtifactStorageError,
        ArtifactValidationError,
        RunpodApiError,
        RunpodConfigurationError,
        RunpodTimeoutError,
    ) as exc:
        return _fail_job(job_id, str(exc), log_prefix="RunPod")

    if result["status"] != RunpodServerlessClient.SUCCESS:
        return _fail_job(
            job_id,
            str(result.get("error") or f"RunPod job finished as {result['status']}"),
            log_prefix="RunPod",
        )
    try:
        schema_json, rows, payload_digest = _output_descriptor(result.get("output"))
        verified_output = store.read_arrow(
            job.tenant_id,
            output_artifact_key,
            expected_digest=payload_digest,
            expected_schema_json=schema_json,
            expected_rows=rows,
        )
    except (ArtifactStorageError, ArtifactValidationError, ValueError) as exc:
        return _fail_job(job_id, str(exc), log_prefix="RunPod")

    job.refresh_from_db()
    if job.status == Job.Status.CANCELED:
        return {"status": "canceled"}
    job.output_artifact_key = verified_output.artifact_key
    job.output_schema_json = verified_output.schema_json
    job.output_rows = verified_output.rows
    job.output_payload_digest = verified_output.payload_digest
    job.status = Job.Status.SUCCEEDED
    job.ended_at = dj_timezone.now()
    _append_log(job, f"[RunPod] completed {submitted.job_id}")
    job.save()
    _post_provenance(
        job,
        engine="runpod-serverless",
        agent_name="theorem-control-plane",
        code_ref=settings.RUNPOD_WORKER_IMAGE_DIGEST,
    )
    return {
        "status": "succeeded",
        "output_payload_digest": payload_digest,
        "runpod_job_id": submitted.job_id,
    }


def _run_r_offload(job_id: str) -> dict[str, str]:
    """Execute implemented R operations from tenant-bound Arrow artifacts."""
    from apps.orchestration.models import Job

    job = Job.objects.get(id=job_id)
    if job.status == Job.Status.CANCELED:
        return {"status": "canceled"}
    try:
        from apps.orchestration.r_runtime import runtime_identity

        identity = runtime_identity()
        lockfile_hash = renv_code_ref()
        if job.tenant_id is None:
            raise ArtifactValidationError("R jobs require an admitted tenant")
        if job.operation != "data_science.r.survey_weight":
            return _fail_job(
                job_id,
                f"R runtime is ready ({identity}; renv={lockfile_hash}), but no real runner is "
                f"installed for {job.operation}",
                log_prefix="R",
            )

        store = ArtifactStore.from_settings()
        input_artifact_key = str(job.kwargs_json.get("input_artifact_key") or "")
        input_table = store.read_table(
            job.tenant_id,
            input_artifact_key,
            expected_digest=job.input_payload_digest,
            expected_schema_json=str(job.kwargs_json.get("input_schema_json") or ""),
            expected_rows=job.kwargs_json.get("input_rows"),
        )
        for column_name in ("value", "weight"):
            if column_name not in input_table.column_names:
                raise ArtifactValidationError(
                    "survey_weight requires Arrow columns value and weight"
                )
        values_column = input_table["value"].combine_chunks()
        weights_column = input_table["weight"].combine_chunks()
        numeric_types = (pa.types.is_integer, pa.types.is_floating)
        if not any(check(values_column.type) for check in numeric_types) or not any(
            check(weights_column.type) for check in numeric_types
        ):
            raise ArtifactValidationError("survey_weight value and weight must be numeric Arrow columns")
        values = values_column.cast(pa.float64()).to_pylist()
        weights = weights_column.cast(pa.float64()).to_pylist()
        if (
            any(value is None or not math.isfinite(value) for value in values)
            or any(weight is None or not math.isfinite(weight) or weight < 0 for weight in weights)
            or not any(weight > 0 for weight in weights)
        ):
            raise ArtifactValidationError(
                "survey_weight requires finite values and non-negative weights with a positive sum"
            )

        from rpy2 import robjects

        weighted_mean = float(
            robjects.r["weighted.mean"](
                robjects.FloatVector(values),
                robjects.FloatVector(weights),
            )[0]
        )
        if not math.isfinite(weighted_mean):
            raise ArtifactValidationError("R weighted.mean returned a non-finite result")
        output_table = pa.table(
            {
                "weighted_mean": pa.array([weighted_mean], type=pa.float64()),
                "input_rows": pa.array([input_table.num_rows], type=pa.int64()),
            }
        )
        output_artifact = store.write_table(
            job.tenant_id,
            store.output_key(job.tenant_id, job.operation_id),
            output_table,
        )
    except (ArtifactConfigurationError, ArtifactStorageError, ArtifactValidationError, RuntimeError) as exc:
        return _fail_job(job_id, str(exc), log_prefix="R")

    job.refresh_from_db()
    if job.status == Job.Status.CANCELED:
        return {"status": "canceled"}
    job.output_artifact_key = output_artifact.artifact_key
    job.output_schema_json = output_artifact.schema_json
    job.output_rows = output_artifact.rows
    job.output_payload_digest = output_artifact.payload_digest
    job.status = Job.Status.SUCCEEDED
    job.ended_at = dj_timezone.now()
    _append_log(job, f"[R] completed survey_weight ({identity})")
    job.save()
    _post_provenance(
        job,
        engine="rpy2",
        agent_name="R",
        code_ref=lockfile_hash,
    )
    return {
        "status": "succeeded",
        "output_payload_digest": output_artifact.payload_digest,
    }


@shared_task(name="apps.orchestration.tasks.cancel_job_task")
def cancel_job_task(job_id: str) -> dict:
    from apps.orchestration.models import Job
    from celery import current_app

    job = Job.objects.filter(id=job_id).first()
    if job is None:
        return {"error": "missing"}
    if job.celery_task_id:
        current_app.control.revoke(job.celery_task_id, terminate=False)
    remote_job_id = (job.kwargs_json.get("runpod") or {}).get("job_id")
    if remote_job_id and settings.OFFLOAD_EXECUTION_MODE == "runpod":
        try:
            RunpodServerlessClient(
                api_key=settings.RUNPOD_API_KEY,
                endpoint_id=settings.RUNPOD_SERVERLESS_ENDPOINT_ID,
                base_url=settings.RUNPOD_API_BASE,
                timeout_seconds=settings.RUNPOD_REQUEST_TIMEOUT_SECONDS,
            ).cancel(str(remote_job_id))
        except (RunpodApiError, RunpodConfigurationError) as exc:
            _append_log(job, f"[RunPod] remote cancel deferred: {exc}")
    job.status = Job.Status.CANCELED
    job.ended_at = dj_timezone.now()
    job.save(update_fields=["status", "ended_at", "logs", "updated_at"])
    return {"status": "canceled"}


@shared_task(name="apps.orchestration.tasks.re_run_job")
def re_run_job(job_id: str) -> dict:
    """Re-enqueue with the same operation_id (idempotency key)."""
    from apps.orchestration.models import Job

    job = Job.objects.get(id=job_id)
    job.status = Job.Status.QUEUED
    job.error = ""
    job.ended_at = None
    job.save(update_fields=["status", "error", "ended_at", "updated_at"])
    result = dispatch_offload.delay(str(job.id))
    job.celery_task_id = result.id or ""
    job.save(update_fields=["celery_task_id"])
    return {"job_id": str(job.id), "celery_task_id": job.celery_task_id}
