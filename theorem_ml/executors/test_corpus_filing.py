"""Numerical contracts and explicitly gated real TabICLv2/Rust corpus oracle."""

import json
import os
from pathlib import Path
import subprocess

import numpy as np
import pytest

from .contracts import validate_layout
from .corpus_filing import CachedTabICLv2, prototype_updates


def test_corpus_probability_weighted_prototypes_and_priors():
    features = np.asarray([[5, 1, 0], [6, 0, 1]], dtype=np.float32)
    probabilities = np.asarray([[0.9, 0.1], [0.2, 0.8]])
    updates = prototype_updates(features, probabilities, ["a", "b"], 1, 2)
    assert [update["prior"] for update in updates] == pytest.approx([0.55, 0.45])
    assert updates[0]["prototype"] == pytest.approx(
        np.array([0.9, 0.2]) / np.linalg.norm([0.9, 0.2])
    )
    assert len(updates[0]["prototype"]) == 2
    with pytest.raises(ValueError):
        prototype_updates(features, [[1, 1], [1, 1]], ["a", "b"], 1, 2)


class RecordingBinaryEstimator:
    """Test double for wrapper call topology, never a model-quality oracle."""

    calls = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls.append(self)

    def fit(self, features, labels):
        self.classes_ = np.unique(labels)
        self.labels = labels.copy()

    def predict_proba(self, features):
        return np.tile([0.7, 0.3], (len(features), 1))


def test_corpus_many_class_wrapper_keeps_every_binary_context_kv_cached():
    RecordingBinaryEstimator.calls = []
    wrapper = object.__new__(CachedTabICLv2)
    wrapper._classifier = RecordingBinaryEstimator
    wrapper.model_path, wrapper.device = "explicit-test-double", "cpu"
    wrapper.fit(np.ones((11, 2)), np.asarray([f"class-{index}" for index in range(11)]))
    assert len(wrapper.models) == 11
    assert all(
        model.kwargs["kv_cache"] is True
        and model.kwargs["allow_auto_download"] is False
        for model in wrapper.models
    )
    assert all(set(model.labels) == {0, 1} for model in wrapper.models)
    assert wrapper.predict_proba(np.ones((3, 2))).sum(axis=1) == pytest.approx(
        np.ones(3)
    )


def test_corpus_layout_rejects_offset_or_dimension_drift():
    layout = {
        "object_type": "schema:object-type:tenant-a:item",
        "schema_anchor": "anchor",
        "feature_dim": 3,
        "blocks": [
            {"field": "membrane", "offset": 0, "kind": {"kind": "membrane"}},
            {
                "field": "embedding",
                "offset": 1,
                "kind": {"kind": "embedding", "dim": 2},
            },
        ],
    }
    assert validate_layout(layout, 3) == (1, 2)
    with pytest.raises(ValueError):
        validate_layout(layout, 4)
    layout["blocks"][1]["offset"] = 2
    with pytest.raises(ValueError):
        validate_layout(layout, 3)


def test_corpus_real_784_discovery_tabicl_and_online_head_oracle():
    fixture_path = os.environ.get("THEOREM_INDEX_784_FIXTURE")
    oracle = os.environ.get("THEOREM_INDEX_T1_ORACLE")
    if not fixture_path or not oracle:
        pytest.skip(
            "requires labeled real 784 corpus and Rust T1Head oracle executable"
        )
    from .discovery import discover
    from .registry import execute

    fixture = json.loads(Path(fixture_path).read_text())
    assert len(fixture["discovery"]["items"]) == 784
    tenant = fixture["tenant_id"]
    discovered = discover(fixture["discovery"], tenant=tenant)
    assert discovered["fitness"]["accuracy"] >= fixture["tier1_fixture_accuracy"]
    accepted = fixture["accepted_proposals"]
    proposals = {
        proposal["proposal_id"]: proposal for proposal in discovered["proposals"]
    }
    assert set(accepted) <= set(proposals)
    accepted_members = {
        member: collection
        for proposal_id, collection in accepted.items()
        for member in proposals[proposal_id]["members"]
    }
    assert all(
        accepted_members.get(item["id"]) == item["collection"]
        for item in fixture["corpus"]["items"]
    )
    result = execute("corpus_filing", fixture["corpus"], tenant=tenant)
    receipt = subprocess.run(
        [oracle],
        input=json.dumps(
            {
                "feature_layout": fixture["corpus"]["feature_layout"],
                "updates": result["updates"],
                "held_out": fixture["corpus"]["held_out"],
            }
        ),
        text=True,
        capture_output=True,
        check=True,
    )
    proof = json.loads(receipt.stdout)
    assert proof["generation_after"] == proof["generation_before"] + 1
    assert proof["accuracy"] >= fixture["tier1_fixture_accuracy"]


@pytest.mark.parametrize("class_count", [2, 11])
@pytest.mark.parametrize("with_held_out", [True, False])
def test_corpus_real_tabicl_cached_smoke(
    class_count, with_held_out, tmp_path, monkeypatch
):
    if os.environ.get("THEOREM_INDEX_RUN_TABICL_SMOKE") != "1":
        pytest.skip(
            "set THEOREM_INDEX_RUN_TABICL_SMOKE=1 for real model runtime proof on synthetic data"
        )
    from .registry import execute

    monkeypatch.setenv("MLFLOW_ALLOW_FILE_STORE", "true")
    rng = np.random.default_rng(42)
    width = max(4, class_count)

    def rows(prefix, count):
        return [
            {
                "id": f"{prefix}{index}",
                "tenant_id": "runtime-smoke",
                "collection": f"class-{index % class_count}",
                "features": (
                    np.eye(width)[index % class_count] + rng.normal(0, 0.025, width)
                ).tolist(),
            }
            for index in range(count)
        ]

    payload = {
        "items": rows("context-", max(300, class_count * 30)),
        "held_out": rows("held-", class_count * 4) if with_held_out else [],
        "feature_layout": {
            "object_type": "schema:object-type:runtime-smoke:item",
            "schema_anchor": "explicit-synthetic-runtime-smoke",
            "feature_dim": width,
            "blocks": [
                {
                    "field": "embedding",
                    "offset": 0,
                    "kind": {"kind": "embedding", "dim": width},
                }
            ],
        },
    }
    result = execute(
        "corpus_filing", payload, tenant="runtime-smoke", tracking_uri=tmp_path.as_uri()
    )
    assert len(result["updates"]) == class_count
    assert sum(update["prior"] for update in result["updates"]) == pytest.approx(
        1.0, abs=1e-5
    )
    assert result["implementation"] == (
        "tabiclv2_cached_native" if class_count <= 10 else "tabiclv2_cached_one_vs_rest"
    )
    if with_held_out:
        assert result["fitness"]["accuracy"] >= 0.9
    else:
        assert result["fitness"] is None
        from mlflow import MlflowClient

        run = MlflowClient(tracking_uri=tmp_path.as_uri()).get_run(
            result["executor"]["mlflow_run_id"]
        )
        assert (
            run.data.tags["theorem.fitness_status"]
            == "not_measured_no_held_out_filings"
        )
        assert "held_out_filing_accuracy" not in run.data.metrics
