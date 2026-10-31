from dataclasses import asdict, replace
from uuid import uuid4

import pytest

from apps.orchestration.artifacts import ArtifactValidationError
from tests.test_artifacts import store  # noqa: F401 -- shared fake S3 fixture


def test_web_content_descriptor_is_scoped_verified_and_provider_neutral(store):
    tenant = uuid4()
    payload = "Captured café evidence".encode()
    descriptor = store.write_web_content(
        tenant, "snapshot:one", payload, media_type="text/plain; charset=utf-8"
    )
    assert descriptor.object_key.startswith(f"tenants/{tenant}/web/text/")
    assert descriptor.scope == {"visibility": "tenant", "tenant_id": str(tenant)}
    assert store.read_web_content(tenant, descriptor) == payload
    assert set(asdict(descriptor)) == {
        "digest", "object_key", "media_type", "byte_length", "snapshot_id", "scope", "encoding"
    }
    with pytest.raises(ArtifactValidationError, match="tenant scope"):
        store.read_web_content(uuid4(), descriptor)
    with pytest.raises(ArtifactValidationError, match="digest or length"):
        store.read_web_content(tenant, replace(descriptor, byte_length=1))
    store.client.objects[(store.bucket, descriptor.object_key)] = b"corrupt"
    with pytest.raises(ArtifactValidationError, match="digest or length"):
        store.read_web_content(tenant, descriptor)


@pytest.mark.parametrize("kind", ["raw", "text", "captures"])
def test_web_content_namespaces_are_deterministic(store, kind):
    tenant = uuid4()
    left = store.write_web_content(tenant, "capture:a", b"content", media_type="text/plain", kind=kind)
    right = store.write_web_content(tenant, "capture:b", b"content", media_type="text/plain", kind=kind)
    assert left.object_key == right.object_key
    assert f"/web/{kind}/" in left.object_key
