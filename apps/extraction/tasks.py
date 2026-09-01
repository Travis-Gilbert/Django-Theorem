"""Celery fan-out, fixture replay, and reconciliation for extraction jobs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pyarrow as pa
from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.orchestration.artifacts import ArtifactStore
from apps.orchestration.models import Job as OrchestrationJob
from apps.orchestration.tasks import _post_provenance, dispatch_offload

from .models import ExtractionJob, ExtractionShard
from .planner import ExtractionPlanningError, ShardPlan, plan_shards, replan_shard


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = ROOT / "contracts/theorem.extraction.v1.json"
FIXTURE_PATH = ROOT / "contracts/theorem.extraction.v1.fixture.json"


def _contract() -> dict[str, Any]:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def operation_id(job_id: str, index: int, input_digest: str) -> str:
    return hashlib.sha256(f"{job_id}:{index}:{input_digest}".encode("utf-8")).hexdigest()


def _operation_params(job: ExtractionJob, shard: ExtractionShard) -> dict[str, Any]:
    contract = _contract()
    params = dict(job.params)
    params.update(
        {
            "contract": contract["contract"],
            "chunking": contract["chunking"],
            "model": contract["model"],
            "model_id": params.get("model_id") or contract["model"]["default_id"],
            "prompt_hashes": {
                name: prompt["sha256"] for name, prompt in contract["prompts"].items()
            },
            "schema_hash": contract["schema_sha256"],
            "tenant_id": str(job.tenant_id),
            "job_id": str(job.id),
            "shard": shard.index,
        }
    )
    if job.operation == ExtractionJob.Operation.TYPED and not isinstance(
        params.get("object_type"), Mapping
    ):
        raise ValueError("typed extraction requires params.object_type")
    return params


def _create_orchestration_job(shard: ExtractionShard) -> OrchestrationJob:
    job = shard.job
    orchestration_job = OrchestrationJob.objects.create(
        tenant_id=job.tenant_id,
        operation=f"data_science.extraction.{job.operation}",
        operation_id=operation_id(str(job.id), shard.index, shard.input_digest),
        status=OrchestrationJob.Status.QUEUED,
        input_payload_digest=shard.input_digest,
        kwargs_json={
            "input_artifact_key": shard.input_artifact_key,
            "input_schema_json": shard.input_schema_json,
            "input_rows": shard.input_rows,
            "input_entity_ids": [],
            "params": _operation_params(job, shard),
        },
    )
    shard.orchestration_job = orchestration_job
    shard.save(update_fields=["orchestration_job", "updated_at"])
    return orchestration_job


def _create_shard(job: ExtractionJob, plan: ShardPlan) -> ExtractionShard:
    return ExtractionShard.objects.create(
        job=job,
        index=plan.index,
        input_artifact_key=plan.artifact_key,
        input_digest=plan.payload_digest,
        input_schema_json=plan.schema_json,
        input_rows=plan.rows,
        status=ExtractionShard.Status.QUEUED,
    )


def _dispatch_shard(shard: ExtractionShard) -> None:
    orchestration_job = shard.orchestration_job or _create_orchestration_job(shard)
    result = dispatch_offload.delay(str(orchestration_job.id))
    orchestration_job.celery_task_id = result.id or orchestration_job.celery_task_id
    orchestration_job.save(update_fields=["celery_task_id", "updated_at"])


@shared_task(name="apps.extraction.tasks.submit_extraction")
def submit_extraction(job_id: str) -> dict[str, Any]:
    """Plan immutable inputs and fan out one existing offload job per shard."""
    job = ExtractionJob.objects.select_related("tenant").get(id=job_id)
    if job.status == ExtractionJob.Status.CANCELED:
        return {"status": "canceled"}
    if job.shards.exists():
        return reconcile_extraction(str(job.id))
    source = {
        "job_id": str(job.id),
        "source_kind": job.source_kind,
        "source_ref": job.source_ref,
    }
    plans = plan_shards(
        job.tenant,
        source,
        settings.EXTRACTION_MAX_INPUT_BYTES,
    )
    with transaction.atomic():
        locked = ExtractionJob.objects.select_for_update().get(id=job.id)
        if locked.shards.exists():
            return {"status": locked.status, "reused": True}
        shards = [_create_shard(locked, plan) for plan in plans]
        locked.shard_count = len(shards)
        locked.status = ExtractionJob.Status.RUNNING
        locked.save(update_fields=["shard_count", "status", "updated_at"])
        for shard in shards:
            _create_orchestration_job(shard)
    for shard in shards:
        _dispatch_shard(shard)
    return reconcile_extraction(str(job.id))


def _output_schema(contract: Mapping[str, Any]) -> pa.Schema:
    type_map = {
        "utf8": pa.string(),
        "int32": pa.int32(),
        "int64": pa.int64(),
        "float32": pa.float32(),
    }
    return pa.schema(
        [
            pa.field(
                field["name"],
                type_map[field["type"]],
                nullable=bool(field["nullable"]),
            )
            for field in contract["arrow_schema"]["fields"]
        ]
    )


def _stub_rows(
    orchestration_job: OrchestrationJob,
    input_table: pa.Table,
) -> list[dict[str, Any]]:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    params = orchestration_job.kwargs_json["params"]
    passage_ids = set(input_table["passage_id"].to_pylist())
    if orchestration_job.operation.endswith(".atlas"):
        rows = [
            dict(row)
            for row in fixture["stub_responses"]["atlas_rows"]
            if row["passage_id"] in passage_ids
        ]
    else:
        object_type = params["object_type"]
        label_field = object_type["label_identifier_field"]
        prompt_hash = object_type.get("prompt_hash") or hashlib.sha256(
            object_type["system"].encode("utf-8")
        ).hexdigest()
        schema_hash = object_type.get("schema_hash") or canonical_hash(
            object_type["schema"]
        )
        records_by_passage = fixture["stub_responses"]["typed_records_by_passage"]
        text_by_passage = {
            row["passage_id"]: row["text"] for row in input_table.to_pylist()
        }
        rows = []
        for passage_id in sorted(passage_ids):
            for record in records_by_passage.get(passage_id, []):
                subject = record[label_field]
                start = text_by_passage[passage_id].find(subject)
                rows.append(
                    {
                        "tenant_id": params["tenant_id"],
                        "job_id": params["job_id"],
                        "shard": params["shard"],
                        "stage": "typed",
                        "passage_id": passage_id,
                        "span_start": start if start >= 0 else None,
                        "span_end": start + len(subject) if start >= 0 else None,
                        "subject": subject,
                        "subject_kind": "record",
                        "predicate": "is_a",
                        "object": object_type["object_type_id"],
                        "object_kind": "record",
                        "object_type_id": object_type["object_type_id"],
                        "record_json": json.dumps(
                            record,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        "confidence": None,
                        "model_id": params["model_id"],
                        "prompt_hash": prompt_hash,
                        "schema_hash": schema_hash,
                        "extractor_version": "theorem.extraction.v1",
                    }
                )
    for row in rows:
        row["tenant_id"] = params["tenant_id"]
        row["job_id"] = params["job_id"]
        row["shard"] = params["shard"]
        row["model_id"] = params["model_id"]
    return sorted(
        rows,
        key=lambda row: (
            row["stage"],
            row["passage_id"],
            row["subject"],
            row["predicate"],
            row["object"],
        ),
    )


def complete_extraction_stub(job_id: str) -> dict[str, Any]:
    """Replay fixture output only under OFFLOAD_EXECUTION_MODE=stub."""
    if settings.OFFLOAD_EXECUTION_MODE != "stub":
        raise RuntimeError("fixture extraction is available only in stub mode")
    job = OrchestrationJob.objects.get(id=job_id)
    if job.status == OrchestrationJob.Status.CANCELED:
        return {"status": "canceled"}
    store = ArtifactStore.from_settings()
    input_table = store.read_table(
        job.tenant_id,
        job.kwargs_json["input_artifact_key"],
        expected_digest=job.input_payload_digest,
        expected_schema_json=job.kwargs_json["input_schema_json"],
        expected_rows=job.kwargs_json["input_rows"],
    )
    rows = _stub_rows(job, input_table)
    contract = _contract()
    output = store.write_table(
        job.tenant_id,
        store.output_key(job.tenant_id, job.operation_id),
        pa.Table.from_pylist(rows, schema=_output_schema(contract)),
    )
    job.output_artifact_key = output.artifact_key
    job.output_schema_json = output.schema_json
    job.output_rows = output.rows
    job.output_payload_digest = output.payload_digest
    job.status = OrchestrationJob.Status.SUCCEEDED
    job.ended_at = timezone.now()
    job.logs = f"{job.logs}[FixtureExtraction] replayed {output.rows} rows\n"[-16_000:]
    job.save()
    _post_provenance(
        job,
        engine="fixture",
        agent_name="FixtureExtractionWorker",
        code_ref="theorem.extraction.v1.fixture",
    )
    return {
        "status": "succeeded",
        "output_payload_digest": output.payload_digest,
    }


def _sync_shard(shard: ExtractionShard) -> bool:
    orchestration_job = shard.orchestration_job
    if orchestration_job is None:
        return False
    orchestration_job.refresh_from_db()
    changed = False
    mapped = {
        OrchestrationJob.Status.QUEUED: ExtractionShard.Status.QUEUED,
        OrchestrationJob.Status.RUNNING: ExtractionShard.Status.RUNNING,
        OrchestrationJob.Status.SUCCEEDED: ExtractionShard.Status.SUCCEEDED,
        OrchestrationJob.Status.FAILED: ExtractionShard.Status.FAILED,
        OrchestrationJob.Status.CANCELED: ExtractionShard.Status.CANCELED,
    }[orchestration_job.status]
    if shard.status != mapped:
        shard.status = mapped
        changed = True
    for field, value in (
        ("output_artifact_key", orchestration_job.output_artifact_key),
        ("output_rows", orchestration_job.output_rows),
        ("output_digest", orchestration_job.output_payload_digest),
        ("error", orchestration_job.error),
    ):
        if getattr(shard, field) != value:
            setattr(shard, field, value)
            changed = True
    if changed:
        shard.save()
    return changed


@shared_task(name="apps.extraction.tasks.reconcile_extraction")
def reconcile_extraction(job_id: str) -> dict[str, Any]:
    """Fold offload statuses into one extraction job and split oversize shards."""
    job = ExtractionJob.objects.get(id=job_id)
    if job.status == ExtractionJob.Status.CANCELED:
        return {"status": "canceled"}
    store = None
    dispatched_replacements: list[ExtractionShard] = []
    for shard in list(job.shards.select_related("orchestration_job", "job")):
        if shard.status == ExtractionShard.Status.SUPERSEDED:
            continue
        _sync_shard(shard)
        if (
            shard.status == ExtractionShard.Status.FAILED
            and "output_exceeds_max_bytes" in (shard.error or "")
        ):
            store = store or ArtifactStore.from_settings()
            try:
                replacements = replan_shard(shard, store=store)
            except ExtractionPlanningError as exc:
                shard.error = f"{shard.error}; replan refused: {exc}"
                shard.save(update_fields=["error", "updated_at"])
                orchestration_job = shard.orchestration_job
                if orchestration_job is not None:
                    orchestration_job.error = shard.error
                    orchestration_job.save(update_fields=["error", "updated_at"])
                continue
            for replacement in replacements:
                _create_orchestration_job(replacement)
                dispatched_replacements.append(replacement)
    for replacement in dispatched_replacements:
        _dispatch_shard(replacement)
    active = list(
        job.shards.exclude(status=ExtractionShard.Status.SUPERSEDED).order_by("index")
    )
    for shard in active:
        _sync_shard(shard)
    statuses = {shard.status for shard in active}
    if active and statuses == {ExtractionShard.Status.SUCCEEDED}:
        status = ExtractionJob.Status.SUCCEEDED
    elif ExtractionShard.Status.RUNNING in statuses or ExtractionShard.Status.QUEUED in statuses:
        status = ExtractionJob.Status.RUNNING
    elif statuses and statuses <= {ExtractionShard.Status.CANCELED}:
        status = ExtractionJob.Status.CANCELED
    elif ExtractionShard.Status.SUCCEEDED in statuses:
        status = ExtractionJob.Status.PARTIAL
    else:
        status = ExtractionJob.Status.FAILED
    job.status = status
    job.shard_count = len(active)
    job.rows_total = sum(shard.output_rows or 0 for shard in active)
    job.save(update_fields=["status", "shard_count", "rows_total", "updated_at"])
    return {
        "status": job.status,
        "shards": job.shard_count,
        "rows": job.rows_total,
    }


@shared_task(name="apps.extraction.tasks.sweep_extraction_jobs")
def sweep_extraction_jobs() -> dict[str, int]:
    count = 0
    nonterminal = [ExtractionJob.Status.QUEUED, ExtractionJob.Status.RUNNING]
    for job_id in ExtractionJob.objects.filter(status__in=nonterminal).values_list(
        "id", flat=True
    ):
        reconcile_extraction(str(job_id))
        count += 1
    return {"reconciled": count}
