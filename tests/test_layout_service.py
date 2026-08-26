"""Local contract, policy, determinism, and admission oracles for layout."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from django.test import Client
from django.utils import timezone

from apps.keys.mint import mint_api_key
from apps.layout.cache import clear_memory_cache
from apps.layout.cache import get_cached_response, set_cached_response
from apps.layout.canonical import MAX_LAYOUT_EDGES, MAX_LAYOUT_NODES, canonical_dot
from apps.layout.canonical import validate_graph
from apps.layout.contracts import LayoutEdge, LayoutNode, LayoutRequest, LayoutResponse
from apps.layout.policy import POLICIES, classify_graph, resolve_policy
from apps.layout.service import LayoutExecutionError, _execute_worker, compute_layout
from apps.tenancy.models import Tenant

FIXTURE_PATH = (
    Path(__file__).resolve().parents[1] / "contracts/theorem.layout.v1.fixture.json"
)


def request_payload(*, graph_class="plan_dag"):
    return {
        "contract": "theorem.layout.v1",
        "graph_class": graph_class,
        "nodes": [
            {"id": "V01", "w_px": 96, "h_px": 40, "kind": "verification"},
            {"id": "W01", "w_px": 120, "h_px": 48, "kind": "work"},
        ],
        "edges": [{"id": "e01", "from": "W01", "to": "V01", "kind": "verifies"}],
        "params": {},
    }


def post_json(client, payload):
    return client.post(
        "/internal/layout/compute",
        data=json.dumps(payload),
        content_type="application/json",
    )


def test_versioned_fixture_is_strict_and_round_trips_without_semantic_loss():
    fixture = json.loads(FIXTURE_PATH.read_text())
    request = LayoutRequest.model_validate(fixture["layout_request"])
    response = LayoutResponse.model_validate(fixture["layout_response"])

    assert (
        request.model_dump(mode="json", by_alias=True, exclude_none=True)
        == fixture["layout_request"]
    )
    assert (
        response.model_dump(mode="json", exclude_none=True)
        == fixture["layout_response"]
    )
    assert fixture["fixture_evidence"]["native_layout_oracle"] is False


@pytest.fixture(autouse=True)
def isolated_layout_cache():
    clear_memory_cache()
    yield
    clear_memory_cache()


@pytest.fixture
def admitted_client(db):
    tenant = Tenant.objects.create(
        slug=f"layout-{uuid4()}", display_name="Layout tenant"
    )
    minted = mint_api_key(tenant, scopes=["layout:compute"])
    client = Client(HTTP_AUTHORIZATION=f"Bearer {minted.plaintext}")
    client.tenant = tenant
    client.minted = minted
    return client


@pytest.mark.django_db
def test_compute_is_authenticated_cached_and_byte_deterministic(
    admitted_client, monkeypatch
):
    calls = []

    def deterministic_positions(_dot, engine, node_ids):
        calls.append((engine, node_ids))
        return {
            "V01": {"x": 216.0, "y": 24.0},
            "W01": {"x": 60.0, "y": 24.0},
        }

    monkeypatch.setattr("apps.layout.service.graphviz_version", lambda: "2.42.2")
    monkeypatch.setattr("apps.layout.service._execute_worker", deterministic_positions)

    first = post_json(admitted_client, request_payload())
    second = post_json(admitted_client, request_payload())

    assert first.status_code == 200, first.content
    assert second.status_code == 200, second.content
    assert first.content == second.content
    assert calls == [("dot", ["V01", "W01"])]
    response = first.json()
    assert response["positions"] == [
        {"id": "V01", "x_px": 216.0, "y_px": 24.0},
        {"id": "W01", "x_px": 60.0, "y_px": 24.0},
    ]
    assert response["engine"] == "dot"
    assert response["graphviz_version"] == "2.42.2"
    assert response["policy_id"] == "plan_dag"
    assert response["input_digest"].startswith("sha256:")


@pytest.mark.django_db
def test_cache_is_tenant_scoped(admitted_client, monkeypatch):
    calls = []
    monkeypatch.setattr("apps.layout.service.graphviz_version", lambda: "2.42.2")
    monkeypatch.setattr(
        "apps.layout.service._execute_worker",
        lambda _dot, _engine, node_ids: (
            calls.append(node_ids)
            or {node_id: {"x": 1.0, "y": 2.0} for node_id in node_ids}
        ),
    )
    other = Tenant.objects.create(slug=f"other-{uuid4()}", display_name="Other")
    other_key = mint_api_key(other, scopes=["layout:compute"])
    other_client = Client(HTTP_AUTHORIZATION=f"Bearer {other_key.plaintext}")

    assert post_json(admitted_client, request_payload()).status_code == 200
    assert post_json(other_client, request_payload()).status_code == 200
    assert len(calls) == 2


def test_cache_degrades_to_bounded_process_memory_when_valkey_fails(monkeypatch):
    class FailingClient:
        def get(self, _key):
            raise OSError("Valkey read unavailable")

        def set(self, _key, _value, *, ex):
            assert ex > 0
            raise OSError("Valkey write unavailable")

    monkeypatch.setattr("apps.layout.cache._redis_client", lambda: FailingClient())
    monkeypatch.setattr("apps.layout.cache.settings.LAYOUT_MEMORY_CACHE_MAX_ENTRIES", 2)

    set_cached_response("tenant", "one", b"one")
    set_cached_response("tenant", "two", b"two")
    assert get_cached_response("tenant", "one") == b"one"
    set_cached_response("tenant", "three", b"three")

    assert get_cached_response("tenant", "one") == b"one"
    assert get_cached_response("tenant", "two") is None
    assert get_cached_response("tenant", "three") == b"three"


@pytest.mark.django_db
def test_machine_key_admission_fails_closed(admitted_client, monkeypatch):
    monkeypatch.setattr("apps.layout.service.graphviz_version", lambda: "2.42.2")
    monkeypatch.setattr(
        "apps.layout.service._execute_worker",
        lambda _dot, _engine, node_ids: {
            node_id: {"x": 1.0, "y": 2.0} for node_id in node_ids
        },
    )
    assert post_json(Client(), request_payload()).status_code == 401

    weak = mint_api_key(admitted_client.tenant, scopes=["layout:read"])
    weak_client = Client(HTTP_AUTHORIZATION=f"Bearer {weak.plaintext}")
    assert post_json(weak_client, request_payload()).status_code == 403

    admitted_client.minted.api_key.revoked_at = timezone.now()
    admitted_client.minted.api_key.save(update_fields=["revoked_at"])
    assert post_json(admitted_client, request_payload()).status_code == 401

    expired = mint_api_key(
        admitted_client.tenant,
        scopes=["layout:compute"],
        expires_at=timezone.now() - timedelta(seconds=1),
    )
    expired_client = Client(HTTP_AUTHORIZATION=f"Bearer {expired.plaintext}")
    assert post_json(expired_client, request_payload()).status_code == 401

    admitted_client.tenant.is_active = False
    admitted_client.tenant.save(update_fields=["is_active"])
    active_key = mint_api_key(admitted_client.tenant, scopes=["layout:compute"])
    inactive_client = Client(HTTP_AUTHORIZATION=f"Bearer {active_key.plaintext}")
    assert post_json(inactive_client, request_payload()).status_code == 403


def test_policy_is_data_driven_and_dot_is_canonical():
    body = LayoutRequest.model_validate(request_payload())
    graph_class, policy, focus_id = resolve_policy(
        body.graph_class, body.nodes, body.edges, body.params
    )
    forward = canonical_dot(body.nodes, body.edges, policy, focus_id=focus_id)
    reversed_body = LayoutRequest.model_validate(
        {
            **request_payload(),
            "nodes": list(reversed(request_payload()["nodes"])),
            "edges": list(reversed(request_payload()["edges"])),
        }
    )
    backward = canonical_dot(
        reversed_body.nodes,
        reversed_body.edges,
        policy,
        focus_id=focus_id,
    )

    assert graph_class == "plan_dag"
    assert forward == backward
    assert 'height="0.6666666666666666"' in forward
    assert 'width="1.6666666666666667"' in forward
    assert 'constraint="false", id="e01", label=""' in forward
    assert '{ rank=same; "W01"; "V01"; }' in forward
    assert {row.engine for row in POLICIES.values()} == {
        "dot",
        "sfdp",
        "neato",
        "twopi",
        "circo",
        "osage",
    }


def test_plan_dependency_from_verifier_does_not_merge_the_next_work_rank():
    payload = request_payload()
    payload["nodes"].append({"id": "W02", "w_px": 120, "h_px": 48, "kind": "work"})
    payload["edges"].append(
        {"id": "e02", "from": "V01", "to": "W02", "kind": "dependency"}
    )
    body = LayoutRequest.model_validate(payload)
    _, policy, focus_id = resolve_policy(
        body.graph_class, body.nodes, body.edges, body.params
    )

    dot = canonical_dot(body.nodes, body.edges, policy, focus_id=focus_id)

    assert '{ rank=same; "W01"; "V01"; }' in dot
    assert '{ rank=same; "W02"; "V01"; }' not in dot


def test_absent_graph_class_uses_structural_classifier():
    dag = LayoutRequest.model_validate({**request_payload(), "graph_class": None})
    assert classify_graph(dag.nodes, dag.edges) == "derivation"

    containment = LayoutRequest.model_validate(
        {
            **request_payload(),
            "graph_class": None,
            "edges": [{"id": "e01", "from": "W01", "to": "V01", "kind": "contains"}],
        }
    )
    assert classify_graph(containment.nodes, containment.edges) == "containment"

    cyclic = LayoutRequest.model_validate(
        {
            **request_payload(),
            "graph_class": None,
            "edges": [
                {"id": "e01", "from": "W01", "to": "V01", "kind": "reference"},
                {"id": "e02", "from": "V01", "to": "W01", "kind": "reference"},
            ],
        }
    )
    assert classify_graph(cyclic.nodes, cyclic.edges) == "neighborhood"


@pytest.mark.parametrize("graph_class", sorted(POLICIES))
def test_every_policy_executes_in_the_native_pygraphviz_worker(graph_class):
    payload = request_payload(graph_class=graph_class)
    if graph_class == "ego_radial":
        payload["params"] = {"focus_id": "W01"}
    if graph_class == "containment":
        for node in payload["nodes"]:
            node["cluster"] = "plan"
    body = LayoutRequest.model_validate(payload)

    first = compute_layout(body, tenant_slug=f"native-{graph_class}")
    second = compute_layout(body, tenant_slug=f"native-{graph_class}")
    response = json.loads(first)

    assert first == second
    assert [item["id"] for item in response["positions"]] == ["V01", "W01"]
    assert response["engine"] == POLICIES[graph_class].engine_for(2)
    assert response["graphviz_version"]


def test_neighborhood_policy_switches_engine_at_200_nodes():
    policy = POLICIES["neighborhood"]
    assert policy.engine_for(199) == "neato"
    assert policy.engine_for(200) == "sfdp"


def test_dense_cyclic_classifier_selects_the_large_code_map_policy():
    nodes = [
        {
            "id": f"n{index:03}",
            "w_px": 80,
            "h_px": 32,
            "kind": "symbol",
        }
        for index in range(200)
    ]
    edges = [
        {
            "id": f"e{index:03}-{offset}",
            "from": f"n{index:03}",
            "to": f"n{(index + offset) % 200:03}",
            "kind": "reference",
        }
        for index in range(200)
        for offset in (1, 2)
    ]
    body = LayoutRequest.model_validate(
        {
            "contract": "theorem.layout.v1",
            "nodes": nodes,
            "edges": edges,
            "params": {},
        }
    )

    graph_class, policy, _focus_id = resolve_policy(
        body.graph_class, body.nodes, body.edges, body.params
    )

    assert graph_class == "code_map_large"
    assert policy.engine_for(len(body.nodes)) == "sfdp"


def _admission_nodes(count: int) -> list[LayoutNode]:
    return [
        LayoutNode(id=f"n{index}", w_px=80, h_px=32, kind="symbol")
        for index in range(count)
    ]


def _admission_edges(count: int) -> list[LayoutEdge]:
    return [
        LayoutEdge(id=f"e{index}", from_="n0", to="n1", kind="reference")
        for index in range(count)
    ]


def test_graph_admission_accepts_exact_end_to_end_budget():
    assert MAX_LAYOUT_NODES == 512
    assert MAX_LAYOUT_EDGES == 4_096

    validate_graph(
        _admission_nodes(MAX_LAYOUT_NODES),
        _admission_edges(MAX_LAYOUT_EDGES),
    )


@pytest.mark.parametrize(
    ("node_count", "edge_count", "expected_message"),
    (
        (513, 0, "between 1 and 512 entries"),
        (512, 4_097, "at most 4096 entries"),
    ),
)
def test_graph_admission_refuses_end_to_end_budget_plus_one(
    node_count, edge_count, expected_message
):
    with pytest.raises(ValueError, match=expected_message):
        validate_graph(
            _admission_nodes(node_count),
            _admission_edges(edge_count),
        )


def test_layout_worker_output_is_capped_before_parent_memory(tmp_path, monkeypatch):
    worker = tmp_path / "oversize_worker.py"
    worker.write_text("print('oversize')\n", encoding="utf-8")
    monkeypatch.setattr("apps.layout.service.WORKER_PATH", worker)
    monkeypatch.setattr("apps.layout.service.settings.LAYOUT_MAX_OUTPUT_BYTES", 4)

    with pytest.raises(LayoutExecutionError, match="size cap"):
        _execute_worker("digraph {}", "dot", [])


@pytest.mark.django_db
def test_invalid_graph_refuses_before_engine_execution(admitted_client, monkeypatch):
    monkeypatch.setattr(
        "apps.layout.service._execute_worker",
        lambda *_args: pytest.fail("invalid graph reached Graphviz"),
    )
    payload = request_payload()
    payload["edges"][0]["to"] = "missing"
    response = post_json(admitted_client, payload)
    assert response.status_code == 422
