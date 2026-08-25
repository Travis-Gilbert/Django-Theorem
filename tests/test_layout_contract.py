"""Pinned-runtime oracle for the exact theorem.layout.v1 fixture."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.layout.cache import clear_memory_cache
from apps.layout.contracts import LayoutRequest
from apps.layout.service import compute_layout, graphviz_version

FIXTURE_PATH = (
    Path(__file__).resolve().parents[1] / "contracts/theorem.layout.v1.fixture.json"
)


def test_pinned_graphviz_cold_recompute_matches_exact_fixture_bytes():
    fixture = json.loads(FIXTURE_PATH.read_text())
    expected = fixture["layout_response"]
    runtime_version = graphviz_version()
    if runtime_version != expected["graphviz_version"]:
        pytest.skip(
            f"requires pinned Graphviz {expected['graphviz_version']}; "
            f"runtime links {runtime_version}"
        )
    request = LayoutRequest.model_validate(fixture["layout_request"])

    clear_memory_cache()
    first = compute_layout(request, tenant_slug="fixture-cold-one")
    clear_memory_cache()
    second = compute_layout(request, tenant_slug="fixture-cold-two")
    expected_bytes = json.dumps(
        expected,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")

    assert first == second
    assert first == expected_bytes
