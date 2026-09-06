"""Policy oracles use explicit metric fixtures, never claimed model evidence."""
import json

from theorem_ml.extract.bench.acceptance import CATEGORIES, POLICY, REQUIRED_METRICS, evaluate


def fixture_measured_rows():
    policy = json.loads(POLICY.read_text())
    rows = []
    for stage, rules in policy["stages"].items():
        metrics = {key: 1. for key in REQUIRED_METRICS[stage]}
        for operation in ("minimum", "strict_minimum", "maximum"):
            metrics.update({metric: 0. if operation == "maximum" else 1. for metric in rules.get(operation, {})})
        for category in CATEGORIES:
            for index in range(10):
                rows.append({"document": f"{category}-{index}", "stage": stage, "category": category,
                             "status": "measured", "metrics": dict(metrics), "source_sha256": "fixture"})
    return rows


def test_full_measured_low_quality_is_failure_not_missing_evidence():
    rows = fixture_measured_rows()
    assert evaluate(rows)["status"] == "passed"
    for row in rows:
        row["metrics"] = {key: .01 for key in row["metrics"]}
    result = evaluate(rows)
    assert result["status"] == "failed_quality" and result["failed_quality"] > 0
    assert result["missing_evidence"] == 0
    assert all(row["status"] == "measured" for row in rows)


def test_atom_threshold_is_strict_and_empty_sets_cannot_pass():
    rows = fixture_measured_rows()
    for row in rows:
        if row["stage"] == "claims.decompose":
            row["metrics"].update(atom_stability=.99, nonempty_atom_set=0.)
    failed = {c["name"] for c in evaluate(rows)["checks"] if c["status"] == "failed_quality"}
    assert {"claims.decompose.atom_stability", "claims.decompose.nonempty_atom_set"} <= failed


def test_multicolumn_reference_gate_and_table_subset_cannot_be_diluted():
    rows = fixture_measured_rows()
    for row in rows:
        if row["stage"] == "parse.paddle" and row["category"] == "multi_column":
            row["metrics"]["reading_order"] = .96
        if row["stage"] == "parse.paddle" and row["category"] == "merged_table":
            row["metrics"]["table_structure_f1"] = 0
    checks = {c["name"]: c["status"] for c in evaluate(rows)["checks"]}
    assert checks["parse.paddle.reading_order"] == "passed"
    assert checks["O2.2_multicolumn_reading_order"] == "failed_quality"
    assert checks["parse.paddle.merged_table.table_structure_f1"] == "failed_quality"


def test_missing_metric_and_category_are_missing_evidence():
    rows = fixture_measured_rows()
    rows[0]["category"] = "invented"
    next(r for r in rows if r["stage"] == "spans.gliner2")["metrics"].pop("span_f1")
    assert evaluate(rows)["status"] == "missing_evidence"


def test_real_mlflow_keeps_measured_quality_failure_and_cli_fails(tmp_path, monkeypatch):
    """Metric fixtures drive the real CLI projection and SQLite publication."""
    from mlflow import MlflowClient
    from theorem_ml.extract.bench import __main__ as cli
    rows = fixture_measured_rows()
    for row in rows:
        row["metrics"] = {key: .01 for key in row["metrics"]}
    uri = "sqlite:///" + str(tmp_path / "mlflow.db")
    monkeypatch.setattr(cli, "run", lambda args: rows)
    monkeypatch.setattr("sys.argv", ["bench", "--all", "--tenant", "metric-fixture",
                                    "--tracking-uri", uri, "--output", str(tmp_path / "output")])
    assert cli.main() == 1
    acceptance = json.loads((tmp_path / "output/acceptance.json").read_text())
    assert acceptance["status"] == "failed_quality" and acceptance["missing_evidence"] == 0
    run_ids = json.loads((tmp_path / "output/mlflow-runs.json").read_text())
    run = MlflowClient(tracking_uri=uri).get_run(run_ids["spans.gliner2"])
    assert run.info.status == "FINISHED"
    assert run.data.tags["theorem.acceptance"] == "failed_quality"
    assert abs(run.data.metrics["span_f1"] - .01) < 1e-12
    assert run.data.metrics["missing_count"] == 0
