"""Local production-boundary oracles for the graph layout service spec.

The board fixture and Valkey process are deterministic local evidence. The
artifact-store test is opt-in because it requires an actual local S3-compatible
service; it deliberately refuses hosted endpoints and test doubles.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import httpx
import pytest
import redis
from django.test import override_settings

from apps.layout.cache import cache_key, clear_memory_cache
from apps.layout.contracts import LayoutRequest
from apps.layout.service import compute_layout, graphviz_version
from apps.orchestration.artifacts import (
    ArtifactStore,
    ArtifactValidationError,
    sha256_digest,
)
from tests.layout_oracles import assert_plan_dependency_flow

SPEC_FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "contracts/theorem.layout.v1.agent-chat-plan.fixture.json"
)
SPEC_SOURCE_COMMIT = "f125a04118fce2e7a971b89d663d24a4cf2caa43"
LOCAL_S3_ENV = (
    "THEOREM_LAYOUT_LOCAL_S3_ENDPOINT_URL",
    "THEOREM_LAYOUT_LOCAL_S3_ACCESS_KEY_ID",
    "THEOREM_LAYOUT_LOCAL_S3_SECRET_ACCESS_KEY",
    "THEOREM_LAYOUT_LOCAL_S3_REGION",
    "THEOREM_LAYOUT_LOCAL_S3_BUCKET_PREFIX",
)


def _agent_chat_request() -> tuple[dict, LayoutRequest]:
    fixture = json.loads(SPEC_FIXTURE_PATH.read_text(encoding="utf-8"))
    return fixture, LayoutRequest.model_validate(fixture["layout_request"])


def test_plan_flow_oracle_rejects_a_collapsed_dependency_rank():
    payload = {
        "edges": [
            {"from": "W01", "to": "V01", "kind": "verifies"},
            {"from": "V01", "to": "W02", "kind": "dependency"},
        ]
    }
    positions = {
        "W01": {"x_px": 100.0},
        "V01": {"x_px": 100.0},
        "W02": {"x_px": 100.0},
    }

    with pytest.raises(AssertionError):
        assert_plan_dependency_flow(payload, positions)


def _unused_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        return int(reservation.getsockname()[1])


@contextmanager
def _isolated_valkey():
    executable = os.environ.get("THEOREM_TEST_VALKEY_SERVER", "").strip()
    executable = executable or shutil.which("valkey-server")
    if not executable:
        pytest.skip("valkey-server is required for the local cache oracle")

    port = _unused_loopback_port()
    with tempfile.TemporaryDirectory(prefix="theorem-layout-valkey-") as data_dir:
        process = subprocess.Popen(
            [
                executable,
                "--bind",
                "127.0.0.1",
                "--protected-mode",
                "yes",
                "--port",
                str(port),
                "--dir",
                data_dir,
                "--appendonly",
                "no",
                "--save",
                "",
                "--daemonize",
                "no",
                "--loglevel",
                "warning",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        client = redis.Redis(host="127.0.0.1", port=port, db=0)
        try:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    output = process.stdout.read() if process.stdout else ""
                    pytest.fail(f"local Valkey exited before readiness: {output}")
                try:
                    if client.ping():
                        break
                except redis.RedisError:
                    time.sleep(0.02)
            else:
                pytest.fail("local Valkey did not become ready within 5 seconds")
            yield f"redis://127.0.0.1:{port}/0", client
        finally:
            client.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5.0)
            if process.stdout:
                process.stdout.close()


def test_exact_agent_chat_board_uses_plan_dag_native_layout():
    fixture, request = _agent_chat_request()
    evidence = fixture["fixture_evidence"]

    assert evidence["evidence_class"] == "exact_local_plan_projection_fixture"
    assert evidence["native_layout_oracle"] is True
    assert evidence["source_plan"] == "THEOREMWEB-AGENT-CHAT-1.0"
    assert evidence["source_generation"] == 15
    assert evidence["source_commit"] == SPEC_SOURCE_COMMIT
    assert len(request.nodes) == 31
    assert len(request.edges) == 44
    assert request.graph_class == "plan_dag"
    canonical_request = json.dumps(
        fixture["layout_request"],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    assert evidence["layout_request_digest"] == (
        f"sha256:{hashlib.sha256(canonical_request).hexdigest()}"
    )
    assert (
        request.model_dump(mode="json", by_alias=True, exclude_none=True)
        == fixture["layout_request"]
    )

    clear_memory_cache()
    first = compute_layout(request, tenant_slug="agent-chat-board-cold-a")
    clear_memory_cache()
    second = compute_layout(request, tenant_slug="agent-chat-board-cold-b")

    assert first == second
    response = json.loads(first)
    assert response["engine"] == "dot"
    assert response["policy_id"] == "plan_dag"
    positions = {item["id"]: item for item in response["positions"]}
    assert set(positions) == {node.id for node in request.nodes}
    verify_edges = [edge for edge in request.edges if edge.kind == "verifies"]
    assert len(verify_edges) == 12
    assert_plan_dependency_flow(fixture["layout_request"], positions)


def test_real_local_valkey_cold_warm_and_version_sensitive_keys(monkeypatch):
    _, request = _agent_chat_request()
    tenant_slug = f"layout-local-valkey-{uuid4()}"
    native_version = graphviz_version()

    with _isolated_valkey() as (url, client):
        server = client.info(section="server")
        assert server.get("server_name") == "valkey"
        with override_settings(VALKEY_URL=url, REDIS_URL=""):
            cold = compute_layout(request, tenant_slug=tenant_slug)
            cold_digest = json.loads(cold)["input_digest"].removeprefix("sha256:")
            cold_key = cache_key(tenant_slug, cold_digest)
            assert bytes(client.get(cold_key)) == cold

            real_worker = __import__(
                "apps.layout.service", fromlist=["_execute_worker"]
            )._execute_worker
            monkeypatch.setattr(
                "apps.layout.service._execute_worker",
                lambda *_args: pytest.fail("warm request bypassed Valkey"),
            )
            assert compute_layout(request, tenant_slug=tenant_slug) == cold

            client.delete(cold_key)
            monkeypatch.setattr("apps.layout.service._execute_worker", real_worker)
            recomputed = compute_layout(request, tenant_slug=tenant_slug)
            assert recomputed == cold
            assert bytes(client.get(cold_key)) == recomputed

            monkeypatch.setattr(
                "apps.layout.service.graphviz_version",
                lambda: f"{native_version}-cache-key-variant",
            )
            versioned = compute_layout(request, tenant_slug=tenant_slug)
            versioned_digest = json.loads(versioned)["input_digest"].removeprefix(
                "sha256:"
            )
            versioned_key = cache_key(tenant_slug, versioned_digest)

            assert versioned_key != cold_key
            assert json.loads(versioned)["graphviz_version"].endswith(
                "-cache-key-variant"
            )
            assert bytes(client.get(versioned_key)) == versioned
            assert sorted(
                key.decode() for key in client.keys(f"t:{tenant_slug}:layout:*")
            ) == sorted([cold_key, versioned_key])


def _local_s3_configuration() -> dict[str, str]:
    values = {name: os.environ.get(name, "").strip() for name in LOCAL_S3_ENV}
    if not all(values.values()):
        pytest.skip(
            "set all THEOREM_LAYOUT_LOCAL_S3_* variables for an actual local "
            "S3-compatible service"
        )
    endpoint = urlparse(values["THEOREM_LAYOUT_LOCAL_S3_ENDPOINT_URL"])
    if endpoint.scheme not in {"http", "https"} or endpoint.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        pytest.fail("the local artifact oracle refuses non-loopback S3 endpoints")
    return values


def test_production_artifact_store_against_actual_local_s3():
    values = _local_s3_configuration()
    bucket_prefix = values["THEOREM_LAYOUT_LOCAL_S3_BUCKET_PREFIX"]
    bucket = f"{bucket_prefix[:40].strip('-')}-{uuid4().hex[:12]}"

    with override_settings(
        ARTIFACT_S3_ENDPOINT_URL=values["THEOREM_LAYOUT_LOCAL_S3_ENDPOINT_URL"],
        ARTIFACT_S3_ACCESS_KEY_ID=values["THEOREM_LAYOUT_LOCAL_S3_ACCESS_KEY_ID"],
        ARTIFACT_S3_SECRET_ACCESS_KEY=values[
            "THEOREM_LAYOUT_LOCAL_S3_SECRET_ACCESS_KEY"
        ],
        ARTIFACT_S3_REGION=values["THEOREM_LAYOUT_LOCAL_S3_REGION"],
        ARTIFACT_S3_BUCKET=bucket,
        ARTIFACT_PRESIGN_SECONDS=2,
        ARTIFACT_MAX_BYTES=1024 * 1024,
    ):
        store = ArtifactStore.from_settings()
        store.client.create_bucket(Bucket=bucket)
        stored = None
        try:
            tenant_id = uuid4()
            other_tenant_id = uuid4()
            payload = (
                b'<svg xmlns="http://www.w3.org/2000/svg"><title>local</title></svg>'
            )
            stored = store.write_render_artifact(
                tenant_id,
                payload,
                media_type="image/svg+xml",
            )

            digest = hashlib.sha256(payload).hexdigest()
            assert stored.artifact_key == (f"tenants/{tenant_id}/renders/{digest}.svg")
            assert stored.payload_digest == sha256_digest(payload)
            assert store.get_bytes(tenant_id, stored.artifact_key) == payload

            with pytest.raises(ArtifactValidationError, match="tenant prefix"):
                store.get_bytes(other_tenant_id, stored.artifact_key)
            with pytest.raises(ArtifactValidationError, match="tenant prefix"):
                store.presign_get(other_tenant_id, stored.artifact_key)

            descriptor_url = store.presign_get(tenant_id, stored.artifact_key)
            query = parse_qs(urlparse(descriptor_url).query)
            assert query["X-Amz-Expires"] == ["2"]
            immediate = httpx.get(descriptor_url, timeout=5.0)
            immediate.raise_for_status()
            assert immediate.content == payload

            time.sleep(2.2)
            expired = httpx.get(descriptor_url, timeout=5.0)
            assert expired.status_code in {401, 403}
        finally:
            if stored is not None:
                store.client.delete_object(
                    Bucket=bucket,
                    Key=stored.artifact_key,
                )
            store.client.delete_bucket(Bucket=bucket)
