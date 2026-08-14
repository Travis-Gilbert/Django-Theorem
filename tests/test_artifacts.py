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
)


class FakeS3Client:
    def __init__(self):
        self.objects = {}
        self.presigned = []

    def put_object(self, *, Bucket, Key, Body, ContentType):
        self.objects[(Bucket, Key)] = bytes(Body)
        assert ContentType == "application/vnd.apache.arrow.stream"

    def get_object(self, *, Bucket, Key):
        payload = self.objects[(Bucket, Key)]
        return {"ContentLength": len(payload), "Body": BytesIO(payload)}

    def generate_presigned_url(self, method, *, Params, ExpiresIn, HttpMethod):
        self.presigned.append((method, Params, ExpiresIn, HttpMethod))
        return f"https://storage.example/{Params['Key']}?method={method}"


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
    assert store.presign_get(tenant_id, artifact_key).startswith("https://storage.example/")
    assert store.presign_put(tenant_id, artifact_key).endswith("method=put_object")


def test_artifact_store_rejects_cross_tenant_object_keys(store):
    owner, other = uuid4(), uuid4()
    owner_key = store.allocate_input_key(owner)

    with pytest.raises(ArtifactValidationError, match="admitted tenant prefix"):
        store.presign_get(other, owner_key)


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
