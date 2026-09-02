"""Extraction ledger, planning, admission boundaries, and stub fan-out."""

from __future__ import annotations

import base64
import hashlib
import importlib
import json
import uuid
from datetime import timedelta
from io import BytesIO
from pathlib import Path

import httpx
import pyarrow as pa
import pytest
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.extraction.admin import ExtractionReviewAdmin
from apps.extraction.models import (
    ExtractionJob,
    ExtractionReview,
    ExtractionShard,
    latest_reviews_since,
)
from apps.extraction.planner import (
    ExtractionPlanningError,
    ShardPlan,
    _validate_passage_table,
    passage_schema,
    plan_shards,
    replan_shard,
)
from apps.extraction.tasks import (
    _typed_provenance_hashes,
    complete_extraction_stub,
    reconcile_extraction,
    submit_extraction,
)
from apps.extraction.reviews import (
    CANDIDATE_DIGEST_VERSION,
    LEGACY_CANDIDATE_DIGEST_VERSION,
    candidate_digest,
    candidate_digest_for_version,
)
from apps.keys.mint import mint_api_key
from apps.orchestration.artifacts import (
    ARROW_IPC_CONTENT_TYPE,
    ArtifactStorageError,
    ArtifactStore,
    ArtifactValidationError,
    decode_arrow_ipc,
    encode_arrow_ipc,
)
from apps.orchestration.models import Job as OrchestrationJob
from apps.tenancy.models import Tenant


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads(
    (ROOT / "contracts/theorem.extraction.v1.fixture.json").read_text(encoding="utf-8")
)


class MemoryS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_object(self, *, Bucket, Key, Body, ContentType):
        assert ContentType == ARROW_IPC_CONTENT_TYPE
        self.objects[(Bucket, Key)] = bytes(Body)

    def get_object(self, *, Bucket, Key):
        payload = self.objects[(Bucket, Key)]
        return {"ContentLength": len(payload), "Body": BytesIO(payload)}

    def generate_presigned_url(self, method, *, Params, ExpiresIn, HttpMethod):
        return f"https://storage.example/{Params['Key']}?method={method}"

    def delete_object(self, *, Bucket, Key):
        self.objects.pop((Bucket, Key), None)
        return {}


@pytest.fixture
def artifact_store(monkeypatch) -> ArtifactStore:
    store = ArtifactStore(
        endpoint_url="https://storage.example",
        access_key_id="access",
        secret_access_key="secret",
        region="us-east-2",
        bucket="theorem-artifacts",
        presign_seconds=300,
        max_bytes=8 * 1024 * 1024,
        client=MemoryS3Client(),
    )
    monkeypatch.setattr(
        ArtifactStore,
        "from_settings",
        classmethod(lambda cls: store),
    )
    return store


@pytest.fixture
def extraction_client(db):
    tenant = Tenant.objects.create(
        slug=f"extraction-{uuid.uuid4()}",
        display_name="Extraction",
    )
    key = mint_api_key(tenant, scopes=["extraction:*"])
    client = Client(HTTP_AUTHORIZATION=f"Bearer {key.plaintext}")
    client.tenant = tenant
    return client


def _fixture_input_table() -> pa.Table:
    payload = base64.b64decode(FIXTURE["input"]["payload_base64"])
    return decode_arrow_ipc(payload)


def _artifact_source(store: ArtifactStore, tenant: Tenant) -> dict[str, object]:
    key = f"tenants/{tenant.id}/inputs/extraction-fixture.arrow"
    stored = store.write_table(tenant.id, key, _fixture_input_table())
    return {
        "artifact_key": stored.artifact_key,
        "payload_digest": stored.payload_digest,
        "schema_json": stored.schema_json,
        "rows": stored.rows,
    }


def _submit_body(source_ref: dict[str, object]) -> dict[str, object]:
    return {
        "operation": "atlas",
        "source_kind": "artifact",
        "source_ref": source_ref,
        "params": {},
    }


@pytest.mark.django_db
@override_settings(CELERY_TASK_ALWAYS_EAGER=False)
def test_duplicate_submit_reuses_job(extraction_client, artifact_store):
    body = _submit_body(_artifact_source(artifact_store, extraction_client.tenant))

    first = extraction_client.post(
        "/internal/extraction/submit",
        data=json.dumps(body),
        content_type="application/json",
    )
    second = extraction_client.post(
        "/internal/extraction/submit",
        data=json.dumps(body),
        content_type="application/json",
    )

    assert first.status_code == 200, first.content
    assert second.status_code == 200, second.content
    assert first.json()["job_id"] == second.json()["job_id"]
    assert second.json()["idempotent_replay"] is True
    assert ExtractionJob.objects.count() == 1


@pytest.mark.django_db
@override_settings(CELERY_TASK_ALWAYS_EAGER=False)
def test_submit_idempotency_distinguishes_source_kind(extraction_client):
    bodies = [
        {
            "operation": "atlas",
            "source_kind": source_kind,
            "source_ref": {},
            "params": {},
        }
        for source_kind in ("web_corpus", "life_email")
    ]

    responses = [
        extraction_client.post(
            "/internal/extraction/submit",
            data=json.dumps(body),
            content_type="application/json",
        )
        for body in bodies
    ]

    assert [response.status_code for response in responses] == [200, 200]
    assert responses[0].json()["job_id"] != responses[1].json()["job_id"]
    assert ExtractionJob.objects.count() == 2


@pytest.mark.django_db
@override_settings(CELERY_TASK_ALWAYS_EAGER=False)
def test_typed_submit_rejects_malformed_object_type_before_job_creation(
    extraction_client,
):
    response = extraction_client.post(
        "/internal/extraction/submit",
        data=json.dumps(
            {
                "operation": "typed",
                "source_kind": "artifact",
                "source_ref": {},
                "params": {
                    "object_type": {
                        "object_type_id": "type:law-firm",
                        "label_identifier_field": "name",
                        "system": 42,
                        "schema": {"type": "object"},
                    }
                },
            }
        ),
        content_type="application/json",
    )

    assert response.status_code == 400
    assert "system must be a non-empty string" in response.json()["detail"]
    assert ExtractionJob.objects.count() == 0


@pytest.mark.django_db
def test_reverse_migration_preserves_source_kind_collisions():
    tenant = Tenant.objects.create(
        slug=f"rollback-{uuid.uuid4()}",
        display_name="Rollback",
    )
    first = ExtractionJob.objects.create(
        tenant=tenant,
        operation=ExtractionJob.Operation.ATLAS,
        source_kind=ExtractionJob.SourceKind.WEB_CORPUS,
        source_ref={},
        params_hash="3" * 64,
    )
    second = ExtractionJob.objects.create(
        tenant=tenant,
        operation=ExtractionJob.Operation.ATLAS,
        source_kind=ExtractionJob.SourceKind.LIFE_EMAIL,
        source_ref={},
        params_hash="3" * 64,
    )
    migration = importlib.import_module(
        "apps.extraction.migrations."
        "0002_remove_extractionjob_control_extract_job_idempotent_uniq_and_more"
    )

    migration.prepare_legacy_uniqueness(__import__("django").apps.apps, None)

    first.refresh_from_db()
    second.refresh_from_db()
    assert first.source_ref == second.source_ref == {}
    assert first.params_hash == "3" * 64
    assert second.params_hash != "3" * 64


@pytest.mark.django_db
def test_reverse_migration_uses_structural_json_numeric_equality():
    tenant = Tenant.objects.create(
        slug=f"rollback-json-{uuid.uuid4()}",
        display_name="Rollback JSON",
    )
    first = ExtractionJob.objects.create(
        tenant=tenant,
        operation=ExtractionJob.Operation.ATLAS,
        source_kind=ExtractionJob.SourceKind.WEB_CORPUS,
        source_ref={"n": 1},
        params_hash="0" * 64,
    )
    second = ExtractionJob.objects.create(
        tenant=tenant,
        operation=ExtractionJob.Operation.ATLAS,
        source_kind=ExtractionJob.SourceKind.LIFE_EMAIL,
        source_ref={"n": 1.0},
        params_hash="0" * 64,
    )
    migration = importlib.import_module(
        "apps.extraction.migrations."
        "0002_remove_extractionjob_control_extract_job_idempotent_uniq_and_more"
    )

    migration.prepare_legacy_uniqueness(__import__("django").apps.apps, None)

    first.refresh_from_db()
    second.refresh_from_db()
    assert first.source_ref == {"n": 1}
    assert second.source_ref == {"n": 1.0}
    assert first.params_hash == "0" * 64
    assert second.params_hash != "0" * 64


@pytest.mark.django_db
def test_latest_review_supersedes_by_recency(extraction_client):
    tenant = extraction_client.tenant
    digest = "a" * 64
    first = ExtractionReview.objects.create(
        tenant=tenant,
        candidate_digest=digest,
        decision=ExtractionReview.Decision.ACCEPT,
        reviewer="user:first",
    )
    second = ExtractionReview.objects.create(
        tenant=tenant,
        candidate_digest=digest,
        decision=ExtractionReview.Decision.REJECT,
        reviewer="user:second",
    )
    ExtractionReview.objects.filter(id=first.id).update(
        created_at=timezone.now() - timedelta(minutes=2)
    )

    latest = latest_reviews_since(tenant, timezone.now() - timedelta(hours=1))

    assert [review.id for review in latest] == [second.id]
    assert latest[0].decision == ExtractionReview.Decision.REJECT


@pytest.mark.django_db
def test_five_megabyte_source_plans_five_one_megabyte_shards(artifact_store):
    tenant = Tenant.objects.create(slug=f"plan-{uuid.uuid4()}", display_name="Plan")
    table = pa.Table.from_pylist(
        [
            {
                "passage_id": f"passage:{index}",
                "text": chr(97 + index) * 1_000_000,
                "metadata_json": None,
            }
            for index in range(5)
        ],
        schema=passage_schema(),
    )
    source_key = f"tenants/{tenant.id}/inputs/five-megabytes.arrow"
    source = artifact_store.write_table(tenant.id, source_key, table)
    job_id = uuid.uuid4()

    plans = plan_shards(
        tenant,
        {
            "job_id": str(job_id),
            "source_kind": "artifact",
            "source_ref": {
                "artifact_key": source.artifact_key,
                "payload_digest": source.payload_digest,
                "schema_json": source.schema_json,
                "rows": source.rows,
            },
        },
        1024 * 1024,
        store=artifact_store,
    )

    assert len(plans) == 5
    assert all(plan.rows == 1 for plan in plans)
    assert all(plan.byte_length <= 1024 * 1024 for plan in plans)


def test_exact_passage_schema_still_rejects_null_required_values():
    table = pa.Table.from_arrays(
        [
            pa.array([None], type=pa.string()),
            pa.array(["body"], type=pa.string()),
            pa.array([None], type=pa.string()),
        ],
        schema=passage_schema(),
    )

    with pytest.raises(ExtractionPlanningError, match="passage_id cannot contain nulls"):
        _validate_passage_table(table)


@pytest.mark.django_db
@override_settings(THEOREM_MACHINE_KEY_PASSAGES="passages-key")
def test_remote_source_is_planned_page_by_page(artifact_store):
    tenant_id = uuid.uuid4()
    cursors = []

    def handler(request: httpx.Request) -> httpx.Response:
        cursors.append(request.url.params.get("cursor"))
        assert 1 <= int(request.url.params["limit"]) <= 1000
        assert request.headers["Authorization"] == "Bearer passages-key"
        if request.url.params.get("cursor") is None:
            return httpx.Response(
                200,
                json={
                    "passages": [
                        {"passage_id": "p1", "text": "one"},
                        {"passage_id": "p2", "text": "two"},
                    ],
                    "next_cursor": "page-2",
                },
            )
        return httpx.Response(
            200,
            json={"passages": [{"passage_id": "p3", "text": "three"}]},
        )

    with httpx.Client(
        base_url="https://theorem.example",
        transport=httpx.MockTransport(handler),
    ) as client:
        plans = plan_shards(
            tenant_id,
            {
                "job_id": str(uuid.uuid4()),
                "source_kind": "web_corpus",
                "source_ref": {},
            },
            64 * 1024,
            store=artifact_store,
            http_client=client,
        )

    assert cursors == [None, "page-2"]
    assert [plan.rows for plan in plans] == [2, 1]


@pytest.mark.django_db
@override_settings(THEOREM_MACHINE_KEY_PASSAGES="passages-key")
def test_remote_source_refuses_an_oversized_page(artifact_store):
    client = httpx.Client(
        base_url="https://theorem.example",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "passages": [
                        {"passage_id": "too-large", "text": "x" * 70_000}
                    ]
                },
            )
        ),
    )
    try:
        with pytest.raises(ExtractionPlanningError, match="response byte limit"):
            plan_shards(
                uuid.uuid4(),
                {
                    "job_id": str(uuid.uuid4()),
                    "source_kind": "life_email",
                    "source_ref": {},
                },
                512,
                store=artifact_store,
                http_client=client,
            )
    finally:
        client.close()


@pytest.mark.django_db
@override_settings(THEOREM_MACHINE_KEY_PASSAGES="passages-key")
def test_remote_source_retries_with_smaller_pages_before_sharding(artifact_store):
    passages = [
        {"passage_id": f"p{index}", "text": "x" * 3_000}
        for index in range(40)
    ]
    requested_limits = []

    def handler(request: httpx.Request) -> httpx.Response:
        limit = int(request.url.params["limit"])
        requested_limits.append(limit)
        offset = int(request.url.params.get("cursor", "0"))
        page = passages[offset : offset + limit]
        next_offset = offset + len(page)
        payload = {"passages": page}
        if next_offset < len(passages):
            payload["next_cursor"] = str(next_offset)
        return httpx.Response(200, json=payload)

    with httpx.Client(
        base_url="https://theorem.example",
        transport=httpx.MockTransport(handler),
    ) as client:
        plans = plan_shards(
            uuid.uuid4(),
            {
                "job_id": str(uuid.uuid4()),
                "source_kind": "web_corpus",
                "source_ref": {},
            },
            32 * 1024,
            store=artifact_store,
            http_client=client,
        )

    assert requested_limits[:3] == [64, 32, 16]
    assert sum(plan.rows for plan in plans) == 40
    assert all(plan.byte_length <= 32 * 1024 for plan in plans)


@pytest.mark.django_db
@override_settings(THEOREM_MACHINE_KEY_PASSAGES="passages-key")
def test_later_remote_page_failure_cleans_attempt_artifacts(artifact_store):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("cursor") is None:
            return httpx.Response(
                200,
                json={
                    "passages": [{"passage_id": "p1", "text": "valid"}],
                    "next_cursor": "broken-page",
                },
            )
        return httpx.Response(200, json={"passages": "not-a-list"})

    with httpx.Client(
        base_url="https://theorem.example",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ExtractionPlanningError, match="must return a list"):
            plan_shards(
                uuid.uuid4(),
                {
                    "job_id": str(uuid.uuid4()),
                    "source_kind": "life_email",
                    "source_ref": {},
                },
                64 * 1024,
                store=artifact_store,
                http_client=client,
            )

    assert artifact_store.client.objects == {}


@pytest.mark.django_db
@override_settings(THEOREM_MACHINE_KEY_PASSAGES="passages-key")
def test_remote_source_wraps_invalid_utf8_as_planning_failure(artifact_store):
    with httpx.Client(
        base_url="https://theorem.example",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"\xff")
        ),
    ) as client:
        with pytest.raises(ExtractionPlanningError, match="listing request failed"):
            plan_shards(
                uuid.uuid4(),
                {
                    "job_id": str(uuid.uuid4()),
                    "source_kind": "web_corpus",
                    "source_ref": {},
                },
                64 * 1024,
                store=artifact_store,
                http_client=client,
            )


@pytest.mark.django_db
def test_refused_shard_replans_into_two_and_is_superseded(artifact_store):
    tenant = Tenant.objects.create(slug=f"replan-{uuid.uuid4()}", display_name="Replan")
    job = ExtractionJob.objects.create(
        tenant=tenant,
        operation=ExtractionJob.Operation.ATLAS,
        source_kind=ExtractionJob.SourceKind.ARTIFACT,
        params_hash="b" * 64,
    )
    table = _fixture_input_table()
    key = f"tenants/{tenant.id}/extraction/{job.id}/in/0.arrow"
    stored = artifact_store.write_table(tenant.id, key, table)
    shard = ExtractionShard.objects.create(
        job=job,
        index=0,
        input_artifact_key=key,
        input_digest=stored.payload_digest,
        input_schema_json=stored.schema_json,
        input_rows=stored.rows,
        status=ExtractionShard.Status.FAILED,
        error="output_exceeds_max_bytes",
    )

    replacements = replan_shard(shard, store=artifact_store)

    shard.refresh_from_db()
    assert shard.status == ExtractionShard.Status.SUPERSEDED
    assert [replacement.input_rows for replacement in replacements] == [1, 2]
    assert [replacement.index for replacement in replacements] == [1, 2]
    assert replan_shard(shard, store=artifact_store) == []


@pytest.mark.django_db
def test_replan_refuses_tampered_input_artifact(artifact_store):
    tenant = Tenant.objects.create(
        slug=f"replan-tamper-{uuid.uuid4()}",
        display_name="Replan tamper",
    )
    job = ExtractionJob.objects.create(
        tenant=tenant,
        operation=ExtractionJob.Operation.ATLAS,
        source_kind=ExtractionJob.SourceKind.ARTIFACT,
        params_hash="9" * 64,
    )
    table = _fixture_input_table()
    key = f"tenants/{tenant.id}/extraction/{job.id}/in/0.arrow"
    stored = artifact_store.write_table(tenant.id, key, table)
    shard = ExtractionShard.objects.create(
        job=job,
        index=0,
        input_artifact_key=stored.artifact_key,
        input_digest=stored.payload_digest,
        input_schema_json=stored.schema_json,
        input_rows=stored.rows,
        status=ExtractionShard.Status.FAILED,
    )
    artifact_store.client.objects[
        (artifact_store.bucket, stored.artifact_key)
    ] = encode_arrow_ipc(table.slice(0, 1))

    with pytest.raises(ArtifactValidationError, match="digest"):
        replan_shard(shard, store=artifact_store)


@pytest.mark.django_db
def test_replan_failure_cleans_partial_replacement_artifacts(
    artifact_store,
    monkeypatch,
):
    tenant = Tenant.objects.create(
        slug=f"replan-cleanup-{uuid.uuid4()}",
        display_name="Replan cleanup",
    )
    job = ExtractionJob.objects.create(
        tenant=tenant,
        operation=ExtractionJob.Operation.ATLAS,
        source_kind=ExtractionJob.SourceKind.ARTIFACT,
        params_hash="1" * 64,
    )
    table = _fixture_input_table()
    key = f"tenants/{tenant.id}/extraction/{job.id}/in/original.arrow"
    stored = artifact_store.write_table(tenant.id, key, table)
    shard = ExtractionShard.objects.create(
        job=job,
        index=0,
        input_artifact_key=stored.artifact_key,
        input_digest=stored.payload_digest,
        input_schema_json=stored.schema_json,
        input_rows=stored.rows,
        status=ExtractionShard.Status.FAILED,
    )
    original_write = artifact_store.write_table
    replacement_writes = 0

    def fail_second_replacement(tenant_id, artifact_key, replacement):
        nonlocal replacement_writes
        replacement_writes += 1
        if replacement_writes == 2:
            raise ArtifactStorageError("second replacement failed")
        return original_write(tenant_id, artifact_key, replacement)

    monkeypatch.setattr(artifact_store, "write_table", fail_second_replacement)

    with pytest.raises(ArtifactStorageError, match="second replacement failed"):
        replan_shard(shard, store=artifact_store)

    shard.refresh_from_db()
    assert shard.status == ExtractionShard.Status.FAILED
    assert job.shards.count() == 1
    assert set(artifact_store.client.objects) == {
        (artifact_store.bucket, stored.artifact_key)
    }


@pytest.mark.django_db
def test_cancel_during_planning_never_creates_or_dispatches_shards(
    artifact_store,
    monkeypatch,
):
    tenant = Tenant.objects.create(
        slug=f"cancel-plan-{uuid.uuid4()}",
        display_name="Cancel during planning",
    )
    job = ExtractionJob.objects.create(
        tenant=tenant,
        operation=ExtractionJob.Operation.ATLAS,
        source_kind=ExtractionJob.SourceKind.WEB_CORPUS,
        params_hash="8" * 64,
    )

    def cancel_while_planning(*args, **kwargs):
        table = _fixture_input_table().slice(0, 1)
        stored = artifact_store.write_table(
            tenant.id,
            f"tenants/{tenant.id}/extraction/{job.id}/in/abandoned/0.arrow",
            table,
        )
        ExtractionJob.objects.filter(id=job.id).update(
            status=ExtractionJob.Status.CANCELED
        )
        return [
            ShardPlan(
                index=0,
                artifact_key=stored.artifact_key,
                payload_digest=stored.payload_digest,
                schema_json=stored.schema_json,
                rows=stored.rows,
                byte_length=len(encode_arrow_ipc(table)),
            )
        ]

    dispatched = []
    monkeypatch.setattr("apps.extraction.tasks.plan_shards", cancel_while_planning)
    monkeypatch.setattr(
        "apps.extraction.tasks._dispatch_shard",
        lambda shard: dispatched.append(shard.id),
    )

    result = submit_extraction(str(job.id))

    job.refresh_from_db()
    assert result == {"status": "canceled"}
    assert job.status == ExtractionJob.Status.CANCELED
    assert job.shards.count() == 0
    assert dispatched == []
    assert artifact_store.client.objects == {}


@pytest.mark.django_db
def test_successful_losing_planner_discards_its_attempt_artifacts(
    artifact_store,
    monkeypatch,
):
    tenant = Tenant.objects.create(
        slug=f"planner-loser-{uuid.uuid4()}",
        display_name="Planner loser",
    )
    job = ExtractionJob.objects.create(
        tenant=tenant,
        operation=ExtractionJob.Operation.ATLAS,
        source_kind=ExtractionJob.SourceKind.WEB_CORPUS,
        params_hash="2" * 64,
    )

    def finish_after_competitor(*args, **kwargs):
        table = _fixture_input_table().slice(0, 1)
        stored = artifact_store.write_table(
            tenant.id,
            f"tenants/{tenant.id}/extraction/{job.id}/in/loser/0.arrow",
            table,
        )
        ExtractionShard.objects.create(
            job=job,
            index=0,
            input_artifact_key=f"tenants/{tenant.id}/winner.arrow",
            input_digest="sha256:" + "1" * 64,
            input_schema_json="{}",
            input_rows=1,
        )
        ExtractionJob.objects.filter(id=job.id).update(
            status=ExtractionJob.Status.RUNNING,
            shard_count=1,
        )
        return [
            ShardPlan(
                index=0,
                artifact_key=stored.artifact_key,
                payload_digest=stored.payload_digest,
                schema_json=stored.schema_json,
                rows=stored.rows,
                byte_length=len(encode_arrow_ipc(table)),
            )
        ]

    monkeypatch.setattr("apps.extraction.tasks.plan_shards", finish_after_competitor)

    result = submit_extraction(str(job.id))

    assert result == {"status": "running", "reused": True}
    assert artifact_store.client.objects == {}


@pytest.mark.django_db
def test_failed_shard_adoption_discards_planned_artifacts(
    artifact_store,
    monkeypatch,
):
    tenant = Tenant.objects.create(
        slug=f"adoption-failure-{uuid.uuid4()}",
        display_name="Adoption failure",
    )
    job = ExtractionJob.objects.create(
        tenant=tenant,
        operation=ExtractionJob.Operation.ATLAS,
        source_kind=ExtractionJob.SourceKind.WEB_CORPUS,
        params_hash="3" * 64,
    )

    def plan_for_failed_adoption(*args, **kwargs):
        table = _fixture_input_table().slice(0, 1)
        stored = artifact_store.write_table(
            tenant.id,
            f"tenants/{tenant.id}/extraction/{job.id}/in/failed/0.arrow",
            table,
        )
        return [
            ShardPlan(
                index=0,
                artifact_key=stored.artifact_key,
                payload_digest=stored.payload_digest,
                schema_json=stored.schema_json,
                rows=stored.rows,
                byte_length=len(encode_arrow_ipc(table)),
            )
        ]

    monkeypatch.setattr("apps.extraction.tasks.plan_shards", plan_for_failed_adoption)
    monkeypatch.setattr(
        "apps.extraction.tasks._create_orchestration_job",
        lambda shard: (_ for _ in ()).throw(RuntimeError("adoption failed")),
    )

    with pytest.raises(RuntimeError, match="adoption failed"):
        submit_extraction(str(job.id))

    job.refresh_from_db()
    assert job.status == ExtractionJob.Status.QUEUED
    assert job.shards.count() == 0
    assert artifact_store.client.objects == {}


@pytest.mark.django_db
def test_planning_failure_is_persisted_and_exposed(extraction_client, monkeypatch):
    job = ExtractionJob.objects.create(
        tenant=extraction_client.tenant,
        operation=ExtractionJob.Operation.ATLAS,
        source_kind=ExtractionJob.SourceKind.WEB_CORPUS,
        params_hash="7" * 64,
    )
    monkeypatch.setattr(
        "apps.extraction.tasks.plan_shards",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ExtractionPlanningError("fixture planning refused")
        ),
    )

    result = submit_extraction(str(job.id))

    job.refresh_from_db()
    response = extraction_client.get(f"/internal/extraction/{job.id}")
    assert result == {"status": "failed", "error": "fixture planning refused"}
    assert job.status == ExtractionJob.Status.FAILED
    assert job.error == "fixture planning refused"
    assert response.status_code == 200
    assert response.json()["error"] == "fixture planning refused"


@pytest.mark.django_db
def test_losing_planner_cannot_fail_a_job_with_existing_shards(monkeypatch):
    tenant = Tenant.objects.create(
        slug=f"planner-race-{uuid.uuid4()}",
        display_name="Planner race",
    )
    job = ExtractionJob.objects.create(
        tenant=tenant,
        operation=ExtractionJob.Operation.ATLAS,
        source_kind=ExtractionJob.SourceKind.WEB_CORPUS,
        params_hash="5" * 64,
    )

    def competing_planner(*args, **kwargs):
        ExtractionShard.objects.create(
            job=job,
            index=0,
            input_artifact_key=f"tenants/{tenant.id}/winner.arrow",
            input_digest="sha256:" + "2" * 64,
            input_schema_json="{}",
            input_rows=1,
        )
        ExtractionJob.objects.filter(id=job.id).update(
            status=ExtractionJob.Status.RUNNING,
            shard_count=1,
        )
        raise ExtractionPlanningError("losing planner failed")

    monkeypatch.setattr("apps.extraction.tasks.plan_shards", competing_planner)

    result = submit_extraction(str(job.id))

    job.refresh_from_db()
    assert result == {"status": "running", "reused": True}
    assert job.status == ExtractionJob.Status.RUNNING
    assert job.error == ""
    assert job.shards.count() == 1


@pytest.mark.django_db
@override_settings(OFFLOAD_EXECUTION_MODE="stub")
def test_stub_typed_hash_rejection_fails_orchestration_and_parent(
    artifact_store,
):
    tenant = Tenant.objects.create(
        slug=f"stub-hash-{uuid.uuid4()}",
        display_name="Stub hash",
    )
    parent = ExtractionJob.objects.create(
        tenant=tenant,
        operation=ExtractionJob.Operation.TYPED,
        source_kind=ExtractionJob.SourceKind.ARTIFACT,
        params_hash="4" * 64,
        status=ExtractionJob.Status.RUNNING,
    )
    stored = artifact_store.write_table(
        tenant.id,
        f"tenants/{tenant.id}/inputs/stub-hash.arrow",
        _fixture_input_table(),
    )
    params = json.loads(json.dumps(FIXTURE["typed_params"]))
    params["tenant_id"] = str(tenant.id)
    params["job_id"] = str(parent.id)
    params["object_type"]["schema_hash"] = "0" * 64
    orchestration_job = OrchestrationJob.objects.create(
        tenant=tenant,
        operation="data_science.extraction.typed",
        operation_id=f"stub-hash-{uuid.uuid4()}",
        input_payload_digest=stored.payload_digest,
        kwargs_json={
            "input_artifact_key": stored.artifact_key,
            "input_schema_json": stored.schema_json,
            "input_rows": stored.rows,
            "params": params,
        },
        status=OrchestrationJob.Status.RUNNING,
    )
    ExtractionShard.objects.create(
        job=parent,
        index=0,
        orchestration_job=orchestration_job,
        input_artifact_key=stored.artifact_key,
        input_digest=stored.payload_digest,
        input_schema_json=stored.schema_json,
        input_rows=stored.rows,
        status=ExtractionShard.Status.RUNNING,
    )

    result = complete_extraction_stub(str(orchestration_job.id))
    reconciled = reconcile_extraction(str(parent.id))

    orchestration_job.refresh_from_db()
    parent.refresh_from_db()
    assert result["status"] == "failed"
    assert "schema_hash" in orchestration_job.error
    assert reconciled["status"] == ExtractionJob.Status.FAILED
    assert parent.status == ExtractionJob.Status.FAILED


@pytest.mark.django_db
def test_unsplittable_oversize_shard_settles_as_failed(artifact_store):
    tenant = Tenant.objects.create(
        slug=f"unsplittable-{uuid.uuid4()}",
        display_name="Unsplittable",
    )
    job = ExtractionJob.objects.create(
        tenant=tenant,
        operation=ExtractionJob.Operation.ATLAS,
        source_kind=ExtractionJob.SourceKind.ARTIFACT,
        params_hash="e" * 64,
        status=ExtractionJob.Status.RUNNING,
    )
    input_table = _fixture_input_table().slice(0, 1)
    input_key = f"tenants/{tenant.id}/extraction/{job.id}/in/0.arrow"
    stored = artifact_store.write_table(tenant.id, input_key, input_table)
    orchestration_job = OrchestrationJob.objects.create(
        tenant=tenant,
        operation="data_science.extraction.atlas",
        operation_id=f"unsplittable-{uuid.uuid4()}",
        status=OrchestrationJob.Status.FAILED,
        error="output_exceeds_max_bytes",
    )
    shard = ExtractionShard.objects.create(
        job=job,
        index=0,
        orchestration_job=orchestration_job,
        input_artifact_key=stored.artifact_key,
        input_digest=stored.payload_digest,
        input_schema_json=stored.schema_json,
        input_rows=stored.rows,
        status=ExtractionShard.Status.FAILED,
        error="output_exceeds_max_bytes",
    )

    result = reconcile_extraction(str(job.id))

    shard.refresh_from_db()
    assert result["status"] == ExtractionJob.Status.FAILED
    assert "one-row shard cannot be split" in shard.error
    assert job.shards.count() == 1


@pytest.mark.django_db(transaction=True)
@override_settings(
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
    OFFLOAD_EXECUTION_MODE="stub",
    EXTRACTION_MAX_INPUT_BYTES=1024 * 1024,
)
def test_eager_stub_fanout_reaches_succeeded_with_descriptors(
    extraction_client,
    artifact_store,
):
    response = extraction_client.post(
        "/internal/extraction/submit",
        data=json.dumps(
            _submit_body(_artifact_source(artifact_store, extraction_client.tenant))
        ),
        content_type="application/json",
    )

    assert response.status_code == 200, response.content
    job = ExtractionJob.objects.get(id=response.json()["job_id"])
    assert job.status == ExtractionJob.Status.SUCCEEDED
    assert job.shard_count == 1
    shard = job.shards.get()
    assert shard.output_artifact_key
    assert shard.output_digest.startswith("sha256:")
    assert shard.output_rows == 8

    status = extraction_client.get(f"/internal/extraction/{job.id}")
    assert status.status_code == 200, status.content
    output = status.json()["shards"][0]["output"]
    assert output["payload_digest"] == shard.output_digest
    assert output["download_url"].startswith("https://storage.example/")


@pytest.mark.django_db
def test_submit_refuses_payload_tenant_assertion(extraction_client, artifact_store):
    body = _submit_body(_artifact_source(artifact_store, extraction_client.tenant))
    body["params"] = {"tenant_id": str(uuid.uuid4())}

    response = extraction_client.post(
        "/internal/extraction/submit",
        data=json.dumps(body),
        content_type="application/json",
    )

    assert response.status_code == 403
    assert ExtractionJob.objects.count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("scope", "method", "path", "body"),
    [
        (
            "extraction:read",
            "post",
            "/internal/extraction/submit",
            {
                "operation": "atlas",
                "source_kind": "artifact",
                "source_ref": {},
                "params": {},
            },
        ),
        ("extraction:submit", "get", "/internal/extraction/{job_id}", None),
        ("extraction:read", "post", "/internal/extraction/{job_id}/cancel", None),
        ("extraction:read", "post", "/internal/extraction/review", []),
        (
            "extraction:review",
            "get",
            "/internal/extraction/review?since=2026-01-01T00:00:00Z",
            None,
        ),
    ],
)
def test_each_route_refuses_a_key_without_its_scope(scope, method, path, body):
    tenant = Tenant.objects.create(slug=f"weak-{uuid.uuid4()}", display_name="Weak")
    key = mint_api_key(tenant, scopes=[scope])
    job = ExtractionJob.objects.create(
        tenant=tenant,
        operation=ExtractionJob.Operation.ATLAS,
        source_kind=ExtractionJob.SourceKind.ARTIFACT,
        params_hash=uuid.uuid4().hex,
    )
    client = Client(HTTP_AUTHORIZATION=f"Bearer {key.plaintext}")
    rendered_path = path.format(job_id=job.id)

    if method == "post":
        response = client.post(
            rendered_path,
            data=json.dumps(body),
            content_type="application/json",
        )
    else:
        response = client.get(rendered_path)

    assert response.status_code == 403, response.content


@pytest.mark.django_db
def test_review_feed_route_is_not_shadowed_by_job_route(extraction_client):
    response = extraction_client.get(
        "/internal/extraction/review?since=2026-01-01T00:00:00Z"
    )

    assert response.status_code == 200, response.content
    assert response.json() == []


@pytest.mark.django_db
def test_review_feed_preserves_legacy_digest_version(extraction_client):
    review = ExtractionReview.objects.create(
        tenant=extraction_client.tenant,
        candidate_digest="e" * 64,
        candidate_digest_version=LEGACY_CANDIDATE_DIGEST_VERSION,
        decision=ExtractionReview.Decision.ACCEPT,
        reviewer="migration:fixture",
    )

    response = extraction_client.get(
        "/internal/extraction/review?since=2026-01-01T00:00:00Z"
    )

    assert response.status_code == 200
    assert response.json()[0]["candidate_digest"] == review.candidate_digest
    assert response.json()[0]["candidate_digest_version"] == 1


@pytest.mark.django_db
def test_review_model_rejects_unsupported_digest_version(extraction_client):
    review = ExtractionReview(
        tenant=extraction_client.tenant,
        candidate_digest="f" * 64,
        candidate_digest_version=3,
        decision=ExtractionReview.Decision.ACCEPT,
        reviewer="user:fixture",
    )

    with pytest.raises(ValidationError) as exc_info:
        review.full_clean()

    assert "candidate_digest_version" in exc_info.value.message_dict


@pytest.mark.django_db
def test_review_api_reports_invalid_merge_as_bad_request(extraction_client):
    response = extraction_client.post(
        "/internal/extraction/review",
        data=json.dumps(
            [
                {
                    "candidate_digest": "d" * 64,
                    "decision": "merge_into",
                    "merge_target_claim_id": None,
                    "reason": "fixture invalid merge",
                }
            ]
        ),
        content_type="application/json",
    )

    assert response.status_code == 400, response.content
    assert ExtractionReview.objects.count() == 0


@pytest.mark.django_db
def test_review_api_persists_explicit_legacy_digest_version(extraction_client):
    response = extraction_client.post(
        "/internal/extraction/review",
        data=json.dumps(
            [
                {
                    "candidate_digest": "c" * 64,
                    "candidate_digest_version": LEGACY_CANDIDATE_DIGEST_VERSION,
                    "decision": "accept",
                }
            ]
        ),
        content_type="application/json",
    )

    assert response.status_code == 200
    review = ExtractionReview.objects.get()
    assert review.candidate_digest_version == LEGACY_CANDIDATE_DIGEST_VERSION


@pytest.mark.django_db
def test_review_api_refuses_unbounded_batches(extraction_client):
    response = extraction_client.post(
        "/internal/extraction/review",
        data=json.dumps(
            [
                {
                    "candidate_digest": f"{index:064x}",
                    "decision": "accept",
                }
                for index in range(501)
            ]
        ),
        content_type="application/json",
    )

    assert response.status_code == 400, response.content
    assert "limited to 500" in response.json()["detail"]
    assert ExtractionReview.objects.count() == 0


@pytest.mark.django_db
def test_admin_review_from_verified_shard_candidate(artifact_store):
    tenant = Tenant.objects.create(slug=f"admin-{uuid.uuid4()}", display_name="Admin")
    job = ExtractionJob.objects.create(
        tenant=tenant,
        operation=ExtractionJob.Operation.ATLAS,
        source_kind=ExtractionJob.SourceKind.ARTIFACT,
        params_hash="c" * 64,
        status=ExtractionJob.Status.SUCCEEDED,
    )
    orchestration_job = OrchestrationJob.objects.create(
        tenant=tenant,
        operation="data_science.extraction.atlas",
        operation_id=f"admin-{uuid.uuid4()}",
        status=OrchestrationJob.Status.SUCCEEDED,
    )
    output_payload = base64.b64decode(
        FIXTURE["expected"]["atlas_output"]["payload_base64"]
    )
    output_table = decode_arrow_ipc(output_payload)
    output_key = f"tenants/{tenant.id}/outputs/admin.arrow"
    stored = artifact_store.write_table(tenant.id, output_key, output_table)
    orchestration_job.output_artifact_key = stored.artifact_key
    orchestration_job.output_schema_json = stored.schema_json
    orchestration_job.output_rows = stored.rows
    orchestration_job.output_payload_digest = stored.payload_digest
    orchestration_job.save()
    shard = ExtractionShard.objects.create(
        job=job,
        index=0,
        orchestration_job=orchestration_job,
        input_artifact_key=f"tenants/{tenant.id}/inputs/admin.arrow",
        input_digest="sha256:" + "0" * 64,
        input_schema_json="{}",
        input_rows=3,
        output_artifact_key=stored.artifact_key,
        output_rows=stored.rows,
        output_digest=stored.payload_digest,
        status=ExtractionShard.Status.SUCCEEDED,
    )
    user = get_user_model().objects.create_superuser(
        username=f"reviewer-{uuid.uuid4()}",
        email="reviewer@example.com",
        password="password",
    )
    client = Client()
    client.force_login(user)
    candidate = output_table.to_pylist()[0]
    digest = candidate_digest(str(tenant.id), candidate)

    response = client.post(
        reverse("admin:extraction_extractionshard_review", args=[shard.pk]),
        data={
            "candidate_digest": digest,
            "decision": ExtractionReview.Decision.REJECT,
            "reason": "source span does not support this relation",
        },
    )

    assert response.status_code == 302
    review = ExtractionReview.objects.get()
    assert review.tenant == tenant
    assert review.job == job
    assert review.candidate_digest == digest
    assert review.decision == ExtractionReview.Decision.REJECT


@pytest.mark.django_db
def test_extraction_review_admin_keeps_existing_ledger_rows_immutable():
    tenant = Tenant.objects.create(
        slug=f"immutable-review-{uuid.uuid4()}",
        display_name="Immutable review",
    )
    review = ExtractionReview.objects.create(
        tenant=tenant,
        candidate_digest="6" * 64,
        decision=ExtractionReview.Decision.ACCEPT,
        reviewer="user:fixture",
    )
    model_admin = ExtractionReviewAdmin(ExtractionReview, admin.site)

    assert set(model_admin.get_readonly_fields(None, review)) == {
        field.name for field in ExtractionReview._meta.fields
    }
    assert model_admin.has_change_permission(None, review) is False
    assert model_admin.has_delete_permission(None, review) is False


def test_typed_candidate_digest_includes_canonical_record_content():
    base = {
        "stage": "typed",
        "passage_id": "passage:1",
        "subject": "Fett Law",
        "predicate": "is_a",
        "object": "object-type:law-firm",
    }
    first = {**base, "record_json": '{"name":"Fett Law","status":"declined"}'}
    reordered = {
        **base,
        "record_json": '{ "status": "declined", "name": "Fett Law" }',
    }
    changed = {
        **base,
        "record_json": '{"name":"Fett Law","status":"accepted"}',
    }

    assert candidate_digest("tenant:1", first) == candidate_digest(
        "tenant:1", reordered
    )
    assert candidate_digest("tenant:1", first) != candidate_digest(
        "tenant:1", changed
    )
    assert candidate_digest_for_version(
        "tenant:1",
        first,
        LEGACY_CANDIDATE_DIGEST_VERSION,
    ) == candidate_digest_for_version(
        "tenant:1",
        changed,
        LEGACY_CANDIDATE_DIGEST_VERSION,
    )
    assert CANDIDATE_DIGEST_VERSION == 2


def test_fixture_typed_hashes_reject_supplied_provenance_mismatch():
    object_type = {
        "system": "Extract a law firm.",
        "schema": {"type": "object"},
        "prompt_hash": "0" * 64,
    }

    with pytest.raises(ValueError, match="prompt_hash"):
        _typed_provenance_hashes(object_type)


@pytest.mark.django_db
def test_admin_review_requires_shard_change_and_review_add_permissions():
    tenant = Tenant.objects.create(
        slug=f"admin-permission-{uuid.uuid4()}",
        display_name="Admin permission",
    )
    job = ExtractionJob.objects.create(
        tenant=tenant,
        operation=ExtractionJob.Operation.ATLAS,
        source_kind=ExtractionJob.SourceKind.ARTIFACT,
        params_hash="f" * 64,
    )
    shard = ExtractionShard.objects.create(
        job=job,
        index=0,
        input_artifact_key=f"tenants/{tenant.id}/inputs/permission.arrow",
        input_digest="sha256:" + "0" * 64,
        input_schema_json="{}",
        input_rows=0,
    )
    user = get_user_model().objects.create_user(
        username=f"limited-reviewer-{uuid.uuid4()}",
        password="password",
        is_staff=True,
    )
    user.user_permissions.add(
        Permission.objects.get(codename="change_extractionshard")
    )
    client = Client()
    client.force_login(user)

    response = client.post(
        reverse("admin:extraction_extractionshard_review", args=[shard.pk]),
        data={
            "candidate_digest": "0" * 64,
            "decision": ExtractionReview.Decision.ACCEPT,
        },
    )

    assert response.status_code == 403
    assert ExtractionReview.objects.count() == 0


@pytest.mark.django_db
def test_extraction_operations_do_not_bypass_parent_ledger():
    from apps.orchestration.api import REGISTERED_OPERATIONS

    assert "data_science.extraction.atlas" not in REGISTERED_OPERATIONS
    assert "data_science.extraction.typed" not in REGISTERED_OPERATIONS


@pytest.mark.django_db
@override_settings(
    OFFLOAD_EXECUTION_MODE="runpod",
    RUNPOD_API_KEY="test-key",
    RUNPOD_EXTRACTION_ENDPOINT_ID="extraction-endpoint",
    RUNPOD_EXTRACTION_IMAGE_DIGEST="ghcr.io/theorem/extraction@sha256:immutable",
    RUNPOD_JOB_TIMEOUT_SECONDS=1,
    RUNPOD_POLL_INTERVAL_SECONDS=0.01,
)
def test_extraction_runpod_routing_and_provenance_cover_prompt_hashes(
    artifact_store,
    monkeypatch,
):
    from apps.orchestration.tasks import run_offload_python

    tenant = Tenant.objects.create(slug=f"route-{uuid.uuid4()}", display_name="Route")
    input_key = f"tenants/{tenant.id}/inputs/routing.arrow"
    input_artifact = artifact_store.write_table(
        tenant.id,
        input_key,
        _fixture_input_table(),
    )
    params = {
        "contract": "theorem.extraction.v1",
        "prompt_hashes": {"system": "a" * 64, "concept": "b" * 64},
        "schema_hash": "c" * 64,
    }
    job = OrchestrationJob.objects.create(
        tenant=tenant,
        operation="data_science.extraction.atlas",
        operation_id=f"route-{uuid.uuid4()}",
        input_payload_digest=input_artifact.payload_digest,
        kwargs_json={
            "input_artifact_key": input_artifact.artifact_key,
            "input_schema_json": input_artifact.schema_json,
            "input_rows": input_artifact.rows,
            "input_entity_ids": [],
            "params": params,
        },
        status=OrchestrationJob.Status.RUNNING,
    )
    output_table = decode_arrow_ipc(
        base64.b64decode(FIXTURE["expected"]["atlas_output"]["payload_base64"])
    )
    state = {}

    class ExtractionRunpodClient:
        SUCCESS = "COMPLETED"

        def __init__(self, **kwargs):
            assert kwargs["endpoint_id"] == "extraction-endpoint"

        def submit(self, payload):
            output = artifact_store.write_table(
                tenant.id,
                payload["output"]["artifact_key"],
                output_table,
            )
            state["output"] = output
            return type(
                "Submitted",
                (),
                {"job_id": "remote-extraction-1", "status": "IN_QUEUE"},
            )()

        def wait(self, job_id, **kwargs):
            assert job_id == "remote-extraction-1"
            output = state["output"]
            return {
                "status": "COMPLETED",
                "output": {
                    "output": {
                        "schema_json": output.schema_json,
                        "rows": output.rows,
                        "payload_digest": output.payload_digest,
                    }
                },
            }

    captured = {}

    def capture_derivation(_self, payload):
        captured["payload"] = payload
        return {"ok": True}

    monkeypatch.setattr(
        "apps.orchestration.tasks.RunpodServerlessClient",
        ExtractionRunpodClient,
    )
    monkeypatch.setattr(
        "bridges.rust_provenance.StubProvenanceClient.post_derivation",
        capture_derivation,
    )

    result = run_offload_python(str(job.id))

    assert result["status"] == "succeeded"
    activity = captured["payload"]["activity"]
    assert activity["code_ref"] == "ghcr.io/theorem/extraction@sha256:immutable"
    assert activity["params_hash"] == hashlib.sha256(
        json.dumps(params, sort_keys=True).encode()
    ).hexdigest()
    job.refresh_from_db()
    assert job.kwargs_json["runpod"]["endpoint_id"] == "extraction-endpoint"
