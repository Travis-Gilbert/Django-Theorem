"""Celery offload task stubs — routed by operation name; idempotent on operation_id.

R queue
-------
Tasks whose operation starts with ``data_science.r.`` (and ``run_offload_r``) are
routed to Celery queue ``offload.r``. Provenance agent name for those activities
is ``"R"`` (SPEC D9). Workers for that queue pin R + renv; light workers stay
on the default queue.
"""

from __future__ import annotations

import hashlib
import json
import logging

from celery import shared_task
from django.utils import timezone as dj_timezone

logger = logging.getLogger(__name__)

TASK_VERSION = "0.1.0-stub"


def _is_r_operation(operation: str) -> bool:
    return operation.startswith("data_science.r.")


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
        Job.objects.filter(operation_id=job.operation_id)
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
        return run_offload_r(str(job.id))
    return run_offload_python(str(job.id))


@shared_task(name="apps.orchestration.tasks.run_offload_python")
def run_offload_python(job_id: str) -> dict:
    """StubPythonOffloadExecutor — no live RunPod; marks success with synthetic digest."""
    return _complete_stub(job_id, engine="celery", agent_name="theorem-control-plane")


@shared_task(name="apps.orchestration.tasks.run_offload_r", queue="offload.r")
def run_offload_r(job_id: str) -> dict:
    """StubROffloadExecutor — queue offload.r; provenance agent name is 'R'."""
    return _complete_stub(job_id, engine="celery", agent_name="R")


def _complete_stub(job_id: str, *, engine: str, agent_name: str) -> dict:
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
                "engine_version": TASK_VERSION,
                "params_hash": params_hash,
                "code_ref": "stub-local",
                "started_at_ms": started_ms,
                "ended_at_ms": ended_ms,
            },
            "agent": {"kind": "tool", "name": agent_name, "version": TASK_VERSION},
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
    return {"status": "succeeded", "output_payload_digest": out_digest}


@shared_task(name="apps.orchestration.tasks.cancel_job_task")
def cancel_job_task(job_id: str) -> dict:
    from apps.orchestration.models import Job
    from celery import current_app

    job = Job.objects.filter(id=job_id).first()
    if job is None:
        return {"error": "missing"}
    if job.celery_task_id:
        current_app.control.revoke(job.celery_task_id, terminate=False)
    job.status = Job.Status.CANCELED
    job.ended_at = dj_timezone.now()
    job.save(update_fields=["status", "ended_at", "updated_at"])
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
