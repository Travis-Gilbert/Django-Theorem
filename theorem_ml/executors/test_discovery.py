"""Local algorithm and real MLflow tests; these are not the private 784 corpus."""

import pytest

from .discovery import discover
from .registry import execute


def local_items():
    return [
        {
            "id": f"{group}{index}",
            "tenant_id": "tenant-a",
            "embedding": vector,
            "title": title,
            "entities": [group],
        }
        for group, vector, title in [
            ("rust", [1.0, 0.0], "Rust compiler toolchain"),
            ("garden", [0.0, 1.0], "Garden seed plants"),
        ]
        for index in range(4)
    ]


def test_discovery_recovers_separated_fixture_and_scores_only_held_out_filings():
    payload = {
        "items": local_items(),
        "neighbors": 2,
        "training_filings": {"rust0": "compiler", "garden0": "plants"},
        "held_out_filings": {"rust3": "compiler", "garden3": "plants"},
    }
    result = discover(payload, tenant="tenant-a")
    assert {frozenset(proposal["members"]) for proposal in result["proposals"]} == {
        frozenset(f"{group}{index}" for index in range(4))
        for group in ["rust", "garden"]
    }
    assert result["fitness"] == {"accuracy": 1.0, "held_out_count": 2}
    assert all(
        proposal["top_terms"] and proposal["top_entities"]
        for proposal in result["proposals"]
    )
    assert (
        discover(
            {**payload, "items": list(reversed(payload["items"]))}, tenant="tenant-a"
        )
        == result
    )


def test_discovery_uses_admitted_rustyred_edges():
    items = [
        {"id": "a", "tenant_id": "tenant-a", "embedding": [1.0, 0.0]},
        {"id": "b", "tenant_id": "tenant-a", "embedding": [0.0, 1.0]},
    ]
    assert len(discover({"items": items}, tenant="tenant-a")["proposals"]) == 2
    edge = {"tenant_id": "tenant-a", "from_id": "a", "to_id": "b", "weight": 2}
    assert (
        len(discover({"items": items, "edges": [edge]}, tenant="tenant-a")["proposals"])
        == 1
    )
    assert (
        len(
            discover(
                {
                    "items": items,
                    "edges": [{**edge, "epistemic_status": "contradicted"}],
                },
                tenant="tenant-a",
            )["proposals"]
        )
        == 2
    )
    entity = {"id": "entity", "tenant_id": "tenant-a", "name": "Shared project"}
    mentions = [
        {"tenant_id": "tenant-a", "from_id": item["id"], "to_id": "entity"}
        for item in items
    ]
    proposals = discover(
        {"items": items, "entity_nodes": [entity], "edges": mentions}, tenant="tenant-a"
    )["proposals"]
    assert len(proposals) == 1
    assert proposals[0]["members"] == ["a", "b"]


def test_discovery_refuses_missing_embeddings_foreign_tenants_and_leaked_labels():
    items = local_items()
    for changed in [
        {**items[0], "embedding": None},
        {**items[0], "embedding": [float("nan"), 0]},
        {**items[0], "tenant_id": "tenant-b"},
    ]:
        with pytest.raises(ValueError):
            discover({"items": [changed]}, tenant="tenant-a")
    with pytest.raises(ValueError, match="disjoint"):
        discover(
            {
                "items": items,
                "training_filings": {"rust0": "x"},
                "held_out_filings": {"rust0": "x"},
            },
            tenant="tenant-a",
        )


def test_discovery_registry_logs_real_mlflow_run_and_proposal_receipts(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MLFLOW_ALLOW_FILE_STORE", "true")
    from mlflow import MlflowClient

    tracking = tmp_path.as_uri()
    result = execute(
        "discovery", {"items": local_items()}, tenant="tenant-a", tracking_uri=tracking
    )
    registration = result["executor"]
    run = MlflowClient(tracking_uri=tracking).get_run(registration["mlflow_run_id"])
    assert registration["tier"] == "Learned"
    assert run.info.status == "FINISHED"
    assert run.data.tags["theorem.tenant"] == "tenant-a"
    assert run.data.tags["theorem.fitness_status"] == "not_measured_no_held_out_filings"
    assert all(
        node["properties"]["state"] == "proposed"
        and node["properties"]["mlflow_run_id"] == run.info.run_id
        for node in result["proposal_nodes"]
    )


def test_discovery_failed_run_is_recorded_in_mlflow(tmp_path, monkeypatch):
    monkeypatch.setenv("MLFLOW_ALLOW_FILE_STORE", "true")
    from mlflow import MlflowClient

    with pytest.raises(ValueError):
        execute(
            "discovery",
            {"items": []},
            tenant="tenant-a",
            tracking_uri=tmp_path.as_uri(),
        )
    client = MlflowClient(tracking_uri=tmp_path.as_uri())
    experiment = client.get_experiment_by_name("theorem-index/tenant-a")
    assert client.search_runs([experiment.experiment_id])[0].info.status == "FAILED"
