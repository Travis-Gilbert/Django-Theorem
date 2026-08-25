from __future__ import annotations

from io import BytesIO
from urllib.parse import urlparse
from uuid import uuid4

import pyarrow as pa
import pytest

from apps.orchestration.artifacts import (
    ArtifactStore,
    ArtifactValidationError,
    arrow_schema_json,
    sha256_digest,
)


class FakeS3Client:
    def __init__(self):
        self.objects = {}
        self.presigned = []
        self.content_types = {}

    def put_object(self, *, Bucket, Key, Body, ContentType):
        self.objects[(Bucket, Key)] = bytes(Body)
        self.content_types[(Bucket, Key)] = ContentType

    def get_object(self, *, Bucket, Key):
        payload = self.objects[(Bucket, Key)]
        return {"ContentLength": len(payload), "Body": BytesIO(payload)}

    def generate_presigned_url(self, method, *, Params, ExpiresIn, HttpMethod):
        self.presigned.append((method, Params, ExpiresIn, HttpMethod))
        return f"https://storage.example/{Params['Key']}?method={method}"

    def delete_object(self, *, Bucket, Key):
        self.objects.pop((Bucket, Key), None)
        return {}


@pytest.fixture
def store():
    return ArtifactStore(
        endpoint_url="https://storage.example",
        access_key_id="access",
        secret_access_key="secret",
        region="us-east-2",
        bucket="theorem-artifacts",
        presign_seconds=300,
        max_bytes=1024 * 1024,
        client=FakeS3Client(),
    )


def test_artifacts_are_tenant_scoped_and_round_trip_as_arrow(store):
    tenant_id = uuid4()
    table = pa.table({"source": ["a", "b"], "target": ["b", "c"]})
    artifact_key = store.allocate_input_key(tenant_id)

    written = store.write_table(tenant_id, artifact_key, table)
    recovered = store.read_arrow(
        tenant_id,
        artifact_key,
        expected_digest=written.payload_digest,
        expected_schema_json=arrow_schema_json(table.schema),
        expected_rows=2,
    )

    assert recovered == written
    assert store.presign_get(tenant_id, artifact_key).startswith(
        "https://storage.example/"
    )
    assert store.presign_put(tenant_id, artifact_key).endswith("method=put_object")


def test_artifact_store_rejects_cross_tenant_object_keys(store):
    owner, other = uuid4(), uuid4()
    owner_key = store.allocate_input_key(owner)

    with pytest.raises(ArtifactValidationError, match="admitted tenant prefix"):
        store.presign_get(other, owner_key)


def test_competence_artifact_keys_are_content_addressed_and_exactly_scoped(store):
    tenant_id, project_id = uuid4(), uuid4()
    digest = "sha256:" + "a" * 64
    key = store.competence_artifact_key(
        tenant_id,
        project_id,
        "candidate:one",
        "scorer_model",
        digest,
    )

    assert key.endswith("/scorer_model/" + "a" * 64)
    assert (
        store.validate_competence_key(tenant_id, project_id, "candidate:one", key)
        == key
    )
    with pytest.raises(ArtifactValidationError, match="project/candidate scope"):
        store.validate_competence_key(tenant_id, project_id, "candidate:two", key)
    with pytest.raises(ArtifactValidationError, match="project/candidate scope"):
        store.validate_competence_key(tenant_id, uuid4(), "candidate:one", key)

    store.delete_competence_artifact(tenant_id, project_id, "candidate:one", key)


def test_competence_artifacts_are_written_read_back_and_content_addressed(store):
    tenant_id, project_id = uuid4(), uuid4()
    payload = b'{"schema":"theorem.competence-scorer.v1"}'
    stored = store.write_competence_artifact(
        tenant_id,
        project_id,
        "candidate:one",
        "scorer_model",
        payload,
        "application/vnd.theorem.competence-scorer+json",
    )

    assert stored.payload_digest == sha256_digest(payload)
    assert stored.byte_length == len(payload)
    assert store.get_bytes(tenant_id, stored.artifact_key) == payload
    assert (
        store.client.content_types[(store.bucket, stored.artifact_key)]
        == "application/vnd.theorem.competence-scorer+json"
    )


@pytest.mark.parametrize(
    "payload,media_type,extension",
    [
        (b"\x89PNG\r\n\x1a\nfixture", "image/png", "png"),
        (b'<svg xmlns="http://www.w3.org/2000/svg"></svg>', "image/svg+xml", "svg"),
    ],
)
def test_render_artifacts_are_content_addressed_and_read_back(
    store, payload, media_type, extension
):
    tenant_id = uuid4()
    stored = store.write_render_artifact(
        tenant_id,
        payload,
        media_type=media_type,
    )

    digest_hex = sha256_digest(payload).removeprefix("sha256:")
    assert stored.artifact_key == f"tenants/{tenant_id}/renders/{digest_hex}.{extension}"
    assert stored.payload_digest == sha256_digest(payload)
    assert store.get_bytes(tenant_id, stored.artifact_key) == payload
    assert store.client.content_types[(store.bucket, stored.artifact_key)] == media_type


def test_presigned_urls_keep_the_bucket_in_the_path_for_neon_endpoints():
    tenant_id = uuid4()
    store = ArtifactStore(
        endpoint_url="https://storage.example",
        access_key_id="access",
        secret_access_key="secret",
        region="us-east-2",
        bucket="theorem-artifacts",
        presign_seconds=300,
        max_bytes=1024 * 1024,
    )
    artifact_key = store.allocate_input_key(tenant_id)

    url = urlparse(store.presign_put(tenant_id, artifact_key))

    assert url.hostname == "storage.example"
    assert url.path == f"/theorem-artifacts/{artifact_key}"
