"""Arrow shard planning for extraction jobs."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import httpx
import pyarrow as pa
from django.conf import settings
from django.db import transaction

from apps.orchestration.artifacts import (
    ArtifactStorageError,
    ArtifactStore,
    encode_arrow_ipc,
)
from apps.tenancy.models import Tenant

from .models import ExtractionJob, ExtractionShard


class ExtractionPlanningError(ValueError):
    """The extraction source cannot be planned without violating the contract."""


REMOTE_PAGE_SIZE = 1_000
REMOTE_ESTIMATED_PASSAGE_BYTES = 1_024


class _RemotePageTooLarge(ExtractionPlanningError):
    """A page should be retried with a smaller requested row limit."""


@dataclass(frozen=True)
class ShardPlan:
    index: int
    artifact_key: str
    payload_digest: str
    schema_json: str
    rows: int
    byte_length: int


def passage_schema() -> pa.Schema:
    return pa.schema(
        [
            pa.field("passage_id", pa.string(), nullable=False),
            pa.field("text", pa.string(), nullable=False),
            pa.field("metadata_json", pa.string(), nullable=True),
        ]
    )


def _tenant_id(tenant: Tenant | UUID) -> UUID:
    return tenant.id if isinstance(tenant, Tenant) else tenant


def _validate_passage_table(table: pa.Table) -> pa.Table:
    expected = passage_schema()
    normalized = table
    if not table.schema.equals(expected, check_metadata=False):
        missing = set(expected.names) - set(table.column_names)
        if missing:
            raise ExtractionPlanningError(
                f"passage source omitted columns: {', '.join(sorted(missing))}"
            )
        try:
            normalized = pa.table(
                [table[name].combine_chunks() for name in expected.names],
                schema=expected,
            )
        except (pa.ArrowException, ValueError, TypeError) as exc:
            raise ExtractionPlanningError(
                "passage source does not match the Arrow schema"
            ) from exc
    if any(value is None for value in normalized["passage_id"].to_pylist()):
        raise ExtractionPlanningError("passage_id cannot contain nulls")
    if any(value is None for value in normalized["text"].to_pylist()):
        raise ExtractionPlanningError("text cannot contain nulls")
    return normalized


def _artifact_source(
    tenant_id: UUID,
    source_ref: Mapping[str, Any],
    store: ArtifactStore,
) -> pa.Table:
    artifact_key = source_ref.get("artifact_key")
    if not isinstance(artifact_key, str) or not artifact_key:
        raise ExtractionPlanningError("artifact source requires artifact_key")
    return _validate_passage_table(
        store.read_table(
            tenant_id,
            artifact_key,
            expected_digest=str(source_ref.get("payload_digest") or ""),
            expected_schema_json=str(source_ref.get("schema_json") or ""),
            expected_rows=source_ref.get("rows"),
        )
    )


def _remote_source_pages(
    tenant_id: UUID,
    source_kind: str,
    *,
    client: httpx.Client | None,
    max_response_bytes: int,
) -> Iterator[pa.Table]:
    if not settings.THEOREM_MACHINE_KEY_PASSAGES:
        raise ExtractionPlanningError("THEOREM_MACHINE_KEY_PASSAGES is required")
    owned_client = client is None
    active_client = client or httpx.Client(
        base_url=settings.THEOREM_API_BASE.rstrip("/"),
        timeout=30.0,
    )
    cursor: str | None = None
    seen_cursors: set[str] = set()
    page_size = max(
        1,
        min(
            REMOTE_PAGE_SIZE,
            max_response_bytes // REMOTE_ESTIMATED_PASSAGE_BYTES,
        ),
    )
    try:
        while True:
            while True:
                query = {
                    "tenant": str(tenant_id),
                    "source": source_kind,
                    "limit": page_size,
                }
                if cursor is not None:
                    query["cursor"] = cursor
                try:
                    with active_client.stream(
                        "GET",
                        "/internal/passages",
                        params=query,
                        headers={
                            "Authorization": (
                                f"Bearer {settings.THEOREM_MACHINE_KEY_PASSAGES}"
                            )
                        },
                    ) as response:
                        response.raise_for_status()
                        encoded = bytearray()
                        for chunk in response.iter_bytes(chunk_size=64 * 1024):
                            if len(encoded) + len(chunk) > max_response_bytes:
                                raise _RemotePageTooLarge(
                                    "passage listing page exceeds the response byte limit"
                                )
                            encoded.extend(chunk)
                    payload = json.loads(encoded)
                    break
                except _RemotePageTooLarge:
                    if page_size == 1:
                        raise
                    page_size = max(1, page_size // 2)
                except (
                    httpx.HTTPError,
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                ) as exc:
                    raise ExtractionPlanningError(
                        "passage listing request failed"
                    ) from exc

            raw_passages = (
                payload.get("passages") if isinstance(payload, Mapping) else payload
            )
            if not isinstance(raw_passages, list):
                raise ExtractionPlanningError("passage listing must return a list")
            passages = []
            for item in raw_passages:
                if not isinstance(item, Mapping):
                    raise ExtractionPlanningError(
                        "passage listing rows must be objects"
                    )
                asserted_tenant = item.get("tenant_id")
                if asserted_tenant is not None and str(asserted_tenant) != str(
                    tenant_id
                ):
                    raise ExtractionPlanningError(
                        "passage listing crossed the admitted tenant"
                    )
                passage_id = item.get("passage_id") or item.get("id")
                text = item.get("text")
                if not isinstance(passage_id, str) or not isinstance(text, str):
                    raise ExtractionPlanningError(
                        "passage rows require passage_id and text"
                    )
                metadata_json = item.get("metadata_json")
                if metadata_json is None and item.get("metadata") is not None:
                    metadata_json = json.dumps(
                        item["metadata"],
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                if metadata_json is not None and not isinstance(metadata_json, str):
                    raise ExtractionPlanningError(
                        "metadata_json must be a string or null"
                    )
                passages.append(
                    {
                        "passage_id": passage_id,
                        "text": text,
                        "metadata_json": metadata_json,
                    }
                )
            yield pa.Table.from_pylist(passages, schema=passage_schema())

            next_cursor = (
                payload.get("next_cursor") if isinstance(payload, Mapping) else None
            )
            if next_cursor is None:
                break
            if not isinstance(next_cursor, str) or not next_cursor:
                raise ExtractionPlanningError(
                    "passage listing next_cursor must be a non-empty string"
                )
            if next_cursor in seen_cursors:
                raise ExtractionPlanningError("passage listing repeated a cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
    finally:
        if owned_client:
            active_client.close()


def _source_tables(
    tenant_id: UUID,
    source: Mapping[str, Any],
    *,
    store: ArtifactStore,
    http_client: httpx.Client | None,
    max_response_bytes: int,
) -> Iterator[pa.Table]:
    source_kind = source.get("source_kind")
    source_ref = source.get("source_ref") or {}
    if not isinstance(source_ref, Mapping):
        raise ExtractionPlanningError("source_ref must be an object")
    if source_kind == "artifact":
        yield _artifact_source(tenant_id, source_ref, store)
        return
    if source_kind in {"web_corpus", "life_email"}:
        yield from _remote_source_pages(
            tenant_id,
            str(source_kind),
            client=http_client,
            max_response_bytes=max_response_bytes,
        )
        return
    raise ExtractionPlanningError(f"unsupported extraction source: {source_kind}")


def _split_table(table: pa.Table, max_input_bytes: int) -> list[pa.Table]:
    if max_input_bytes <= 0:
        raise ExtractionPlanningError("max_input_bytes must be positive")
    if table.num_rows == 0:
        return []
    shards: list[pa.Table] = []
    start = 0
    while start < table.num_rows:
        remaining = table.num_rows - start

        def fits(count: int) -> bool:
            return (
                len(encode_arrow_ipc(table.slice(start, count))) <= max_input_bytes
            )

        low = 0
        high = 1
        while high <= remaining and fits(high):
            low = high
            high *= 2
        high = min(high, remaining + 1)
        while low + 1 < high:
            middle = (low + high) // 2
            if fits(middle):
                low = middle
            else:
                high = middle
        best_count = low
        if best_count == 0:
            raise ExtractionPlanningError(
                f"passage row {start} exceeds EXTRACTION_MAX_INPUT_BYTES"
            )
        shards.append(table.slice(start, best_count))
        start += best_count
    return shards


def _input_key(
    tenant_id: UUID,
    job_id: UUID,
    planning_id: UUID,
    index: int,
) -> str:
    return (
        f"tenants/{tenant_id}/extraction/{job_id}/in/"
        f"{planning_id}/{index}.arrow"
    )


def _write_plan(
    *,
    store: ArtifactStore,
    tenant_id: UUID,
    job_id: UUID,
    planning_id: UUID,
    index: int,
    table: pa.Table,
) -> ShardPlan:
    payload = encode_arrow_ipc(table)
    stored = store.write_table(
        tenant_id,
        _input_key(tenant_id, job_id, planning_id, index),
        table,
    )
    return ShardPlan(
        index=index,
        artifact_key=stored.artifact_key,
        payload_digest=stored.payload_digest,
        schema_json=stored.schema_json,
        rows=stored.rows,
        byte_length=len(payload),
    )


def discard_plans(
    tenant: Tenant | UUID,
    plans: list[ShardPlan],
    *,
    store: ArtifactStore | None = None,
) -> None:
    """Delete the explicit artifacts from one unadopted planning attempt."""
    tenant_id = _tenant_id(tenant)
    active_store = store or ArtifactStore.from_settings()
    first_error = None
    for plan in reversed(plans):
        try:
            active_store.delete_artifact(tenant_id, plan.artifact_key)
        except ArtifactStorageError as exc:
            first_error = first_error or exc
    if first_error is not None:
        raise ArtifactStorageError("artifact storage cleanup failed") from first_error


def plan_shards(
    tenant: Tenant | UUID,
    source: Mapping[str, Any],
    max_input_bytes: int,
    *,
    store: ArtifactStore | None = None,
    http_client: httpx.Client | None = None,
) -> list[ShardPlan]:
    """Materialize bounded Arrow inputs under the extraction job prefix."""
    tenant_id = _tenant_id(tenant)
    try:
        job_id = UUID(str(source["job_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ExtractionPlanningError("source requires a UUID job_id") from exc
    active_store = store or ArtifactStore.from_settings()
    planning_id = uuid4()
    plans: list[ShardPlan] = []
    try:
        for table in _source_tables(
            tenant_id,
            source,
            store=active_store,
            http_client=http_client,
            max_response_bytes=max(max_input_bytes * 2, 64 * 1024),
        ):
            for shard_table in _split_table(table, max_input_bytes):
                plans.append(
                    _write_plan(
                        store=active_store,
                        tenant_id=tenant_id,
                        job_id=job_id,
                        planning_id=planning_id,
                        index=len(plans),
                        table=shard_table,
                    )
                )
        if not plans:
            plans.append(
                _write_plan(
                    store=active_store,
                    tenant_id=tenant_id,
                    job_id=job_id,
                    planning_id=planning_id,
                    index=0,
                    table=pa.Table.from_pylist([], schema=passage_schema()),
                )
            )
    except Exception as planning_error:
        try:
            discard_plans(tenant_id, plans, store=active_store)
        except ArtifactStorageError:
            raise ArtifactStorageError(
                "artifact storage cleanup failed after extraction planning"
            ) from planning_error
        raise
    return plans


def replan_shard(
    shard: ExtractionShard,
    *,
    store: ArtifactStore | None = None,
) -> list[ExtractionShard]:
    """Split one refused shard into two replacements and supersede the original."""
    active_store = store or ArtifactStore.from_settings()
    table = active_store.read_table(
        shard.job.tenant_id,
        shard.input_artifact_key,
        expected_digest=shard.input_digest,
        expected_schema_json=shard.input_schema_json,
        expected_rows=shard.input_rows,
    )
    if table.num_rows < 2:
        raise ExtractionPlanningError("a one-row shard cannot be split further")
    split_at = table.num_rows // 2
    replacements = [table.slice(0, split_at), table.slice(split_at)]
    planning_id = uuid4()
    with transaction.atomic():
        locked_job = ExtractionJob.objects.select_for_update().get(id=shard.job_id)
        locked = ExtractionShard.objects.select_for_update().get(id=shard.id)
        if locked.status == ExtractionShard.Status.SUPERSEDED:
            return []
        next_index = (
            locked_job.shards.order_by("-index")
            .values_list("index", flat=True)
            .first()
            or 0
        ) + 1
        created = []
        for offset, replacement in enumerate(replacements):
            plan = _write_plan(
                store=active_store,
                tenant_id=locked.job.tenant_id,
                job_id=locked.job_id,
                planning_id=planning_id,
                index=next_index + offset,
                table=replacement,
            )
            created.append(
                ExtractionShard.objects.create(
                    job=locked_job,
                    index=plan.index,
                    input_artifact_key=plan.artifact_key,
                    input_digest=plan.payload_digest,
                    input_schema_json=plan.schema_json,
                    input_rows=plan.rows,
                    status=ExtractionShard.Status.QUEUED,
                )
            )
        locked.status = ExtractionShard.Status.SUPERSEDED
        locked.save(update_fields=["status", "updated_at"])
    return created
