"""Quality acceptance is independent of whether an observation was measured."""
from hashlib import sha256
from collections import Counter
import json
import math
from pathlib import Path

POLICY = Path(__file__).with_name("thresholds.json")
CATEGORIES = {k: 10 for k in ("pdf", "scan", "warped_photo", "multi_column", "merged_table",
                            "email", "docx", "pptx", "code", "html")}
PARSE_METRICS = {"exact_text_f1", "text_similarity", "reading_order", "table_structure_f1",
                 "merged_cells_f1", "heading_hierarchy_f1", "span_recoverability"}
REQUIRED_METRICS = {**{stage: PARSE_METRICS for stage in
    ("parse.docling", "parse.paddle", "parse.mineru", "parse.native")},
    "spans.gliner2": {"span_f1", "label.person.f1"}, "spans.relex": {"relation_f1"},
    "fields": {"field_accuracy", "omitted", "hallucinated"},
    "claims.decompose": {"atom_stability", "nonempty_atom_set", "claim_count", "ambiguous_count"},
    "claims.verify.minicheck": {"gold_agreement"}, "resolve.link": {"resolution_accuracy"}}


def evaluate(rows, policy_path=POLICY):
    raw = Path(policy_path).read_bytes()
    policy = json.loads(raw)
    checks = []
    document_ids = {r["document"] for r in rows if r["stage"] == next(iter(policy["stages"]))}

    def check(name, selected, metric, threshold, operation, stage):
        values = [r.get("metrics", {}).get(metric) for r in selected]
        complete = bool(selected) and all(r["status"] == "measured" for r in selected) and all(
            isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in values)
        value = sum(values) / len(values) if complete else None
        passed = complete and {"minimum": lambda: value >= threshold,
            "maximum": lambda: value <= threshold, "strict_minimum": lambda: value > threshold}[operation]()
        checks.append({"name": name, "stage": stage, "metric": metric, "value": value,
            "operator": operation, "threshold": threshold, "documents": len(selected),
            "status": "passed" if passed else "failed_quality" if complete else "missing_evidence"})

    for stage, rules in policy["stages"].items():
        stage_rows = [r for r in rows if r["stage"] == stage]
        complete = len(stage_rows) == 100 and len(document_ids) == 100 and {r["document"] for r in stage_rows} == document_ids and Counter(
            r.get("category") for r in stage_rows) == CATEGORIES and all(
            r["status"] == "measured" and REQUIRED_METRICS[stage] <= r.get("metrics", {}).keys() and all(
                isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                for v in r["metrics"].values()) for r in stage_rows)
        checks.append({"name": stage + ".coverage", "stage": stage,
                       "status": "passed" if complete else "missing_evidence"})
        selected = [r for r in stage_rows if r.get("category") not in rules.get("exclude_categories", [])]
        for operation in ("minimum", "maximum", "strict_minimum"):
            for metric, threshold in rules.get(operation, {}).items():
                check(stage + "." + metric, selected, metric, threshold, operation, stage)
        for category, metrics in rules.get("category_minimum", {}).items():
            for metric, threshold in metrics.items():
                check(stage + "." + category + "." + metric,
                      [r for r in stage_rows if r.get("category") == category],
                      metric, threshold, "minimum", stage)
    for rule in policy["comparisons"]:
        left = {r["document"]: r for r in rows if r["stage"] == rule["stage"] and r.get("category") == rule["category"]}
        right = {r["document"]: r for r in rows if r["stage"] == rule["reference"] and r.get("category") == rule["category"]}
        pairs = []
        if len(left) == 10 and left.keys() == right.keys():
            for document, row in left.items():
                reference = right[document]
                a, b = row.get("metrics", {}).get(rule["metric"]), reference.get("metrics", {}).get(rule["metric"])
                if row["status"] == reference["status"] == "measured" and all(
                    isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in (a, b)):
                    pairs.append(abs(a - b) if "max_difference" in rule else b - a)
        value = sum(pairs) / 10 if len(pairs) == 10 else None
        threshold = rule.get("max_difference", rule.get("max_regression"))
        checks.append({"name": rule["name"], "stage": rule["stage"], "metric": rule["metric"],
            "value": value, "threshold": threshold, "documents": len(pairs),
            "status": "missing_evidence" if value is None else "passed" if value <= threshold else "failed_quality"})
    missing = sum(c["status"] == "missing_evidence" for c in checks)
    failed = sum(c["status"] == "failed_quality" for c in checks)
    return {"policy_version": policy["version"], "policy_sha256": sha256(raw).hexdigest(),
            "status": "missing_evidence" if missing else "failed_quality" if failed else "passed",
            "missing_evidence": missing, "failed_quality": failed, "checks": checks}
