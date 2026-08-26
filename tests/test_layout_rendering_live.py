"""Authenticated deployed smoke tests for the graph layout and rendering lanes.

These tests are intentionally skipped without operator-provided credentials.
They are not part of the deterministic local fixture evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from django.test import override_settings

from apps.layout.cache import cache_key
from apps.layout.contracts import LayoutRequest
from apps.layout.service import compute_layout

LIVE_BASE_URL = os.environ.get("THEOREM_LAYOUT_RENDERING_LIVE_BASE_URL", "").rstrip("/")
LIVE_MACHINE_KEY = os.environ.get("THEOREM_LAYOUT_RENDERING_LIVE_MACHINE_KEY", "")
LIVE_ENABLED = bool(LIVE_BASE_URL and LIVE_MACHINE_KEY)
LIVE_SKIP = "set THEOREM_LAYOUT_RENDERING_LIVE_BASE_URL and _MACHINE_KEY"
LIVE_VALKEY_URL = os.environ.get("THEOREM_LAYOUT_LIVE_VALKEY_URL", "")
CHAT_PLAN_FIXTURE = Path(
    os.environ.get(
        "THEOREM_CHAT_PLAN_LAYOUT_FIXTURE",
        Path(__file__).resolve().parents[1]
        / "contracts/theorem.layout.v1.agent-chat-plan.fixture.json",
    )
)


def _post(path: str, payload: dict) -> httpx.Response:
    return httpx.post(
        f"{LIVE_BASE_URL}{path}",
        headers={"Authorization": f"Bearer {LIVE_MACHINE_KEY}"},
        json=payload,
        timeout=30.0,
    )


@pytest.mark.live
@pytest.mark.skipif(not LIVE_ENABLED, reason=LIVE_SKIP)
def test_deployed_layout_is_authenticated_and_byte_deterministic():
    payload = {
        "contract": "theorem.layout.v1",
        "graph_class": "plan_dag",
        "nodes": [
            {"id": "W01", "w_px": 120, "h_px": 48, "kind": "work"},
            {"id": "V01", "w_px": 96, "h_px": 40, "kind": "verification"},
        ],
        "edges": [
            {"id": "e01", "from": "W01", "to": "V01", "kind": "verifies"}
        ],
        "params": {},
    }

    first = _post("/internal/layout/compute", payload)
    second = _post("/internal/layout/compute", payload)

    first.raise_for_status()
    second.raise_for_status()
    assert first.content == second.content
    body = first.json()
    assert body["contract"] == "theorem.layout.v1"
    assert body["engine"] == "dot"
    assert body["policy_id"] == "plan_dag"
    assert {position["id"] for position in body["positions"]} == {"W01", "V01"}
    assert body["graphviz_version"]
    assert body["input_digest"].startswith("sha256:")


@pytest.mark.live
@pytest.mark.skipif(not LIVE_VALKEY_URL, reason="set THEOREM_LAYOUT_LIVE_VALKEY_URL")
def test_real_valkey_cache_matches_a_cold_recompute():
    import redis

    tenant_slug = f"layout-live-{uuid4()}"
    body = LayoutRequest.model_validate(
        {
            "contract": "theorem.layout.v1",
            "graph_class": "plan_dag",
            "nodes": [
                {"id": "W01", "w_px": 120, "h_px": 48, "kind": "work"},
                {
                    "id": "V01",
                    "w_px": 96,
                    "h_px": 40,
                    "kind": "verification",
                },
            ],
            "edges": [
                {"id": "e01", "from": "W01", "to": "V01", "kind": "verifies"}
            ],
            "params": {},
        }
    )
    client = redis.from_url(LIVE_VALKEY_URL)
    key = ""
    try:
        with override_settings(VALKEY_URL=LIVE_VALKEY_URL, REDIS_URL=""):
            first = compute_layout(body, tenant_slug=tenant_slug)
            digest = json.loads(first)["input_digest"].removeprefix("sha256:")
            key = cache_key(tenant_slug, digest)
            assert bytes(client.get(key)) == first
            client.delete(key)
            second = compute_layout(body, tenant_slug=tenant_slug)
            assert second == first
            assert bytes(client.get(key)) == second
    finally:
        if key:
            client.delete(key)
        client.close()


@pytest.mark.live
@pytest.mark.skipif(not LIVE_ENABLED, reason=LIVE_SKIP)
def test_real_chat_plan_board_reads_left_to_right_with_verify_siblings():
    fixture = json.loads(CHAT_PLAN_FIXTURE.read_text(encoding="utf-8"))
    payload = fixture.get("layout_request", fixture)
    assert len(payload["nodes"]) == 31
    assert len(payload["edges"]) == 44
    assert payload["graph_class"] == "plan_dag"

    first = _post("/internal/layout/compute", payload)
    second = _post("/internal/layout/compute", payload)
    first.raise_for_status()
    second.raise_for_status()
    assert first.content == second.content

    positions = {
        position["id"]: position for position in first.json()["positions"]
    }
    for edge in payload["edges"]:
        source_x = positions[edge["from"]]["x_px"]
        target_x = positions[edge["to"]]["x_px"]
        if edge["kind"] == "verifies":
            assert abs(source_x - target_x) <= 1.0
        else:
            assert target_x >= source_x


@pytest.mark.live
@pytest.mark.skipif(not LIVE_ENABLED, reason=LIVE_SKIP)
@pytest.mark.parametrize(
    "path,payload,content_type,signature",
    [
        (
            "/internal/rendering/plantuml",
            {
                "contract": "theorem.rendering.v1",
                "source": "@startuml\nAlice -> Bob: smoke\n@enduml",
                "format": "svg",
            },
            "image/svg+xml",
            b"<svg",
        ),
        (
            "/internal/rendering/diagrams",
            {
                "contract": "theorem.rendering.v1",
                "source": (
                    "from diagrams import Diagram\n"
                    "from diagrams.generic.blank import Blank\n\n"
                    "with Diagram('Smoke', direction='LR'):\n"
                    "    Blank('layout')\n"
                ),
                "format": "png",
            },
            "image/png",
            b"\x89PNG\r\n\x1a\n",
        ),
    ],
)
def test_deployed_renderer_publishes_digest_verified_bytes(
    path, payload, content_type, signature
):
    response = _post(path, payload)
    response.raise_for_status()
    artifact = response.json()["artifact"]

    download = httpx.get(artifact["download_url"], timeout=30.0)
    download.raise_for_status()
    assert download.content.startswith(signature)
    assert artifact["content_type"] == content_type
    assert artifact["artifact_key"].startswith("tenants/")
    assert "/renders/" in artifact["artifact_key"]
    assert artifact["payload_digest"] == (
        "sha256:" + hashlib.sha256(download.content).hexdigest()
    )
    assert artifact["byte_length"] == len(download.content)
