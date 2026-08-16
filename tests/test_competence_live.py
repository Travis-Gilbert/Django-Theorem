"""Provider-backed competence artifact smoke test for V14/V15 evidence."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from apps.orchestration.artifacts import ArtifactStore

REQUIRED_STORAGE_ENV = (
    "ARTIFACT_S3_ENDPOINT_URL",
    "ARTIFACT_S3_ACCESS_KEY_ID",
    "ARTIFACT_S3_SECRET_ACCESS_KEY",
    "ARTIFACT_S3_REGION",
    "ARTIFACT_S3_BUCKET",
)


@pytest.mark.skipif(
    not all(os.environ.get(name, "").strip() for name in REQUIRED_STORAGE_ENV),
    reason="live Neon S3 competence artifact credentials are not configured",
)
def test_live_competence_artifacts_round_trip_and_cleanup():
    store = ArtifactStore.from_settings()
    tenant_id, project_id = uuid4(), uuid4()
    candidate_id = f"candidate:live-smoke:{uuid4()}"
    inputs = [
        (
            "prior_pack",
            b'{"schema":"theorem.competence-prior-pack.v1","smoke":true}',
            "application/vnd.theorem.prior-pack+json",
        ),
        (
            "scorer_model",
            b'{"schema":"theorem.competence-scorer.v1","smoke":true}',
            "application/vnd.theorem.competence-scorer+json",
        ),
    ]
    written = []
    try:
        for kind, payload, media_type in inputs:
            artifact = store.write_competence_artifact(
                tenant_id,
                project_id,
                candidate_id,
                kind,
                payload,
                media_type,
            )
            written.append(artifact)
            assert store.get_bytes(tenant_id, artifact.artifact_key) == payload
    finally:
        for artifact in written:
            store.delete_competence_artifact(
                tenant_id,
                project_id,
                candidate_id,
                artifact.artifact_key,
            )
