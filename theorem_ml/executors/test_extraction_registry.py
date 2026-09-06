"""Registration and measured-fitness boundary, without model stand-ins."""

from theorem_ml.executors.registry import EXECUTORS, list_executors
import pytest


def test_extraction_executors_are_always_registered_and_unmeasured_is_null(monkeypatch, tmp_path, settings):
    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
    settings.BASE_DIR = tmp_path
    names = {"parse.paddle", "spans.gliner2", "claims.verify.minicheck", "resolve.link"}
    assert names <= EXECUTORS.keys()
    rows = [r for r in list_executors(tenant="tenant-a") if r["name"] in names]
    assert len(rows) == 4
    assert all(r["fitness"] is None and r["fitness_status"] == "not_measured" for r in rows)


def test_real_sqlite_observation_records_failure_and_only_links_measured_harness(tmp_path):
    """Seeded metric values are test fixtures; SQLite persistence is real."""
    from mlflow import MlflowClient
    from theorem_ml.executors.registry import observe_execution
    uri = "sqlite:///" + str(tmp_path / "runs.db")
    client = MlflowClient(tracking_uri=uri)
    experiment = client.create_experiment("theorem-extraction/tenant-a")
    fixture = client.create_run(experiment, tags={"theorem.executor": "spans.gliner2",
        "theorem.evidence": "model_harness", "fixture": "seeded_metric_contract"}, start_time=1)
    client.log_metric(fixture.info.run_id, "span_f1", .73)
    client.set_terminated(fixture.info.run_id)
    # A newer successful runtime and a failed harness cannot replace measurement.
    for evidence, status in [("runtime_observation", "FINISHED"), ("model_harness", "FAILED")]:
        other = client.create_run(experiment, tags={"theorem.executor": "spans.gliner2",
                                                   "theorem.evidence": evidence}, start_time=2)
        client.log_metric(other.info.run_id, "span_f1", 1)
        client.set_terminated(other.info.run_id, status=status)
    state = {}
    with observe_execution("spans.gliner2", {"fixture_input": "Ada"}, tenant="tenant-a",
                           state=state, tracking_uri=uri, evidence_class="fixture_execution"):
        pass
    reference = state["executor_runs"][0]
    assert reference["fitness"] == {"value": .73, "metric": "span_f1", "mlflow_run_id": fixture.info.run_id}
    observed = client.get_run(reference["mlflow_run_id"])
    assert observed.info.status == "FINISHED"
    assert observed.data.tags["theorem.evidence"] == "fixture_execution"
    assert observed.data.metrics["elapsed_seconds"] >= 0
    assert "span_f1" not in observed.data.metrics
    assert next(r for r in list_executors(tenant="tenant-a", tracking_uri=uri)
                if r["name"] == "spans.gliner2")["fitness"] == .73
    with pytest.raises(RuntimeError, match="fixture failure"):
        with observe_execution("resolve.link", {}, tenant="tenant-b", state=state,
                               tracking_uri=uri, evidence_class="fixture_execution"):
            raise RuntimeError("fixture failure")
    failed = state["executor_runs"][-1]
    assert failed["fitness"] is None and failed["status"] == "FAILED"
    assert client.get_run(failed["mlflow_run_id"]).info.status == "FAILED"
