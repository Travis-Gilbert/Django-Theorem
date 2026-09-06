"""Run real extraction measurements: python -m theorem_ml.extract.bench --all.

Missing services, references or gold are reported as missing evidence and cause
a nonzero exit. The authored corpus is structural test data, not a production
quality claim. MinerU remains an externally produced comparison artifact.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
import math
import os
from pathlib import Path

from ..elements import DocElementTree
from ..parse.docling import DoclingParser
from ..parse.paddle import PaddleParser
from ..parse.router import ParserRouter
from ..spans.gliner2 import SpanExtractor
from ..spans.labels import SchemaClient
from .score import atom_stability, f1, score_fields, score_parse
from .acceptance import evaluate
from .bootstrap import bootstrap, OBJECT_TYPE


STAGES = ("parse.docling", "parse.paddle", "parse.mineru", "parse.native",
          "spans.gliner2", "spans.relex", "fields", "claims.decompose",
          "claims.verify.minicheck", "resolve.link")


def load_corpus(path: Path) -> list[dict]:
    records = json.loads((path / "manifest.json").read_text())
    if len(records) != 100 or len({r["id"] for r in records}) != 100:
        raise ValueError("The full harness requires 100 distinct authored documents")
    for row in records:
        for key in ("source", "page_image", "comparison_pdf", "gold", *(["renderer_receipt"] if "renderer_receipt" in row else [])):
            item = (path / row[key]).resolve()
            if not item.is_relative_to(path.resolve()):
                raise ValueError("Corpus artifact escaped its directory")
            if sha256(item.read_bytes()).hexdigest() != row[key + "_sha256"]:
                raise ValueError(f"Corpus digest mismatch: {row['id']}/{key}")
        if "renderer_receipt" in row:
            rendered = json.loads((path / row["renderer_receipt"]).read_text())
            if any(rendered.get(key + "_sha256") != row[key + "_sha256"]
                   for key in ("source", "comparison_pdf", "page_image")):
                raise ValueError("Renderer receipt does not bind the current source/PDF/image")
    return records


def native_gold(record, gold, corpus):
    """The code route preserves the complete authored source as one Code node.

    The comparison PDF intentionally contains prose excerpts. Those excerpts
    are not the expected segmentation or text of the native source-code node.
    """
    if record["category"] == "code":
        return {**gold, "texts": [(corpus / record["source"]).read_text(encoding="utf-8")],
                "headings": [], "tables": [], "merged_cells": []}
    return gold


def markdown_projection(rows: list[dict]) -> str:
    lines = ["# Extraction measurements", "", "Corpus: 100 authored fixtures. Missing evidence is not a passing score.", "",
             "| Document | Stage | Status | Metric | Value |", "|---|---|---|---|---|"]
    for row in rows:
        metrics = row.get("metrics", {})
        if not metrics:
            lines.append(f"| {row['document']} | {row['stage']} | {row['status']} | — | — |")
        for name, value in metrics.items():
            lines.append(f"| {row['document']} | {row['stage']} | {row['status']} | {name} | {value:.6f} |")
    return "\n".join(lines) + "\n"


def publish(rows: list[dict], *, tenant: str, tracking_uri: str, output: Path, acceptance=None) -> dict:
    from mlflow import MlflowClient
    from mlflow.exceptions import MlflowException
    if len(rows) != 100 * len(STAGES) or any(
        len([r for r in rows if r["stage"] == stage]) != 100 or
        {r["document"] for r in rows if r["stage"] == stage} !=
        {r["document"] for r in rows if r["stage"] == STAGES[0]} or
        len({r["document"] for r in rows if r["stage"] == stage}) != 100 for stage in STAGES
    ):
        raise ValueError("Publishing requires one row per document per stage across all 100 documents")
    acceptance = acceptance or evaluate(rows)
    client = MlflowClient(tracking_uri=tracking_uri)
    name = f"theorem-extraction/{tenant}"
    experiment = client.get_experiment_by_name(name)
    if experiment is None:
        try:
            experiment_id = client.create_experiment(name)
        except MlflowException:
            experiment = client.get_experiment_by_name(name)
            if experiment is None:
                raise
            experiment_id = experiment.experiment_id
    else:
        experiment_id = experiment.experiment_id
    run_ids = {}
    for stage in STAGES:
        stage_rows = [r for r in rows if r["stage"] == stage]
        run = client.create_run(experiment_id, tags={"theorem.tenant": tenant,
            "theorem.executor": stage, "theorem.corpus": "authored_fixture_100",
            "theorem.evidence": "model_harness", "theorem.corpus_digest": sha256(
                json.dumps([(r["document"], r["source_sha256"]) for r in stage_rows], sort_keys=True).encode()).hexdigest()})
        run_id = run.info.run_id
        checks = [c for c in acceptance["checks"] if c["stage"] == stage]
        quality_failed = any(c["status"] == "failed_quality" for c in checks)
        evidence_missing = any(c["status"] == "missing_evidence" for c in checks)
        client.set_tag(run_id, "theorem.acceptance", "missing_evidence" if evidence_missing else "failed_quality" if quality_failed else "passed")
        client.set_tag(run_id, "theorem.threshold_policy", acceptance["policy_sha256"])
        run_ids[stage] = run_id
        measured = [r for r in stage_rows if r["status"] == "measured"]
        client.log_metric(run_id, "document_count", len(stage_rows))
        client.log_metric(run_id, "measured_count", len(measured))
        client.log_metric(run_id, "missing_count", len(stage_rows) - len(measured))
        names = {key for r in measured for key in r.get("metrics", {})}
        for metric in names:
            values = [r["metrics"][metric] for r in measured if metric in r["metrics"]]
            client.log_metric(run_id, metric, sum(values) / len(values))
        client.log_artifact(run_id, str(output / "results.jsonl"))
        client.log_artifact(run_id, str(output / "results.md"))
        if (output / "acceptance.json").exists():
            client.log_artifact(run_id, str(output / "acceptance.json"))
        # FINISHED means measurements completed. Quality has its own explicit
        # status: a poor measured score remains available as honest fitness.
        client.set_terminated(run_id, status="FINISHED" if len(measured) == len(stage_rows) else "FAILED")
    return run_ids


def run(args) -> list[dict]:
    records = load_corpus(args.corpus)
    if not args.tenant or not args.object_type:
        raise ValueError("--tenant and --object-type name an authenticated benchmark declaration")
    schema = SchemaClient()
    native_parser = DoclingParser()
    page_parser = PaddleParser()
    router = ParserRouter(docling=native_parser, paddle=page_parser)
    extractor = SpanExtractor(schema)
    from ..spans.relex import RelexEvaluator
    relex = RelexEvaluator()
    decomposer = verifier = None
    rows = []
    entity_gold = json.loads(args.entity_gold.read_text()) if args.entity_gold else None
    if entity_gold and entity_gold.get("tenant") != args.tenant:
        raise ValueError("Resolution gold belongs to another tenant")
    for record in records:
        gold = json.loads((args.corpus / record["gold"]).read_text())
        state = {}
        for stage in STAGES:
            row = {"document": record["id"], "stage": stage,
                   "source_sha256": record["source_sha256"], "corpus_class": "authored_fixture",
                   "category": record["category"],
                   "status": "missing_evidence", "metrics": {}}
            try:
                if stage == "parse.docling":
                    tree = native_parser.parse(args.corpus / record["comparison_pdf"], record["id"])
                    row["metrics"] = score_parse(tree, gold)
                    row["input_sha256"] = record["comparison_pdf_sha256"]
                elif stage == "parse.paddle":
                    tree = page_parser.parse(args.corpus / record["page_image"], record["id"])
                    row["metrics"] = score_parse(tree, gold)
                    row["input_sha256"] = record["page_image_sha256"]
                    row["input_provenance"] = record["page_image_provenance"]
                elif stage == "parse.mineru":
                    if args.mineru_dir is None:
                        raise RuntimeError("Supply --mineru-dir with real normalized MinerU output")
                    reference = json.loads((args.mineru_dir / (record["id"] + ".json")).read_text())
                    if reference.get("source_sha256") != record["comparison_pdf_sha256"] or not reference.get("model_version"):
                        raise ValueError("MinerU reference requires matching input digest and model version")
                    row["metrics"] = score_parse(DocElementTree.from_dict(reference["tree"]), gold)
                    row["reference_model"] = reference["model_version"]
                    row["input_sha256"] = record["comparison_pdf_sha256"]
                elif stage == "parse.native":
                    parsed = router.parse(args.corpus / record["source"], record["id"])
                    state["tree"] = parsed.tree
                    row["metrics"] = score_parse(parsed.tree, native_gold(record, gold, args.corpus))
                    row["parser_receipt"] = parsed.receipt.to_dict()
                elif stage == "spans.gliner2":
                    result = extractor.extract(state["tree"], args.tenant, [args.object_type])
                    state["spans"] = result
                    predictions = [(s.field_key, s.text) for s in result.spans]
                    expected = [(key, value) for key, values in gold["spans"].items() for value in values]
                    row["metrics"] = {"span_f1": f1(predictions, expected)["f1"]}
                    for key in sorted(set(gold["spans"]) | {s.field_key for s in result.spans}):
                        row["metrics"][f"label.{key}.f1"] = f1([s.text for s in result.spans if s.field_key == key], gold["spans"].get(key, []))["f1"]
                elif stage == "spans.relex":
                    text = state["tree"].text
                    result = relex.predict([text], ["person", "document"], ["approved"])
                    predicted = [(r["head"]["text"], r["relation"], r["tail"]["text"]) for r in result["relations"][0]]
                    row["metrics"] = {"relation_f1": f1(predicted, [tuple(r) for r in gold["relations"]])["f1"]}
                elif stage == "fields":
                    result = schema.call(args.tenant, "extract_fields", object_type=args.object_type,
                        document=state["tree"].to_dict(), spans=[s.global_span(state["tree"]) for s in state["spans"].spans])
                    fields = result.get("properties", result.get("record", {}).get("properties"))
                    if not isinstance(fields, dict):
                        raise ValueError("Field executor omitted coerced record properties")
                    if entity_gold is None:
                        raise RuntimeError("Run --bootstrap-schema or supply tenant entity gold before scoring relation fields")
                    expected_fields = {k: dict(v) for k, v in gold["fields"].items()}
                    name = gold["fields"]["person"]["value"]
                    entity_id = entity_gold.get("entities", {}).get(name)
                    if entity_id is None:
                        candidates = {m["entity_id"] for mentions in entity_gold["documents"].values()
                                      for m in mentions if m["text"] == name}
                        if len(candidates) != 1:
                            raise ValueError("Relation field gold requires an unambiguous real tenant entity identity")
                        entity_id = candidates.pop()
                    expected_fields["person"] = {"class": "exact", "value": entity_id}
                    scored = score_fields(fields, expected_fields)
                    row["relation_gold_identity"] = entity_id
                    row["field_results"] = scored["fields"]
                    row["metrics"] = {"field_accuracy": scored["accuracy"], "omitted": scored["omitted"], "hallucinated": scored["hallucinated"]}
                elif stage == "claims.decompose":
                    if decomposer is None:
                        from ..claims.decompose import Decomposer
                        decomposer = Decomposer()
                    runs = [decomposer.decompose(state["tree"], derive_claims=True) for _ in range(3)]
                    def embed(texts):
                        return [decomposer.nlp(t).tensor.mean(axis=0) for t in texts]
                    row["metrics"] = {"atom_stability": atom_stability([[c.text for c in r.claims] for r in runs], embed),
                                      "nonempty_atom_set": float(all(r.claims for r in runs)),
                                      "claim_count": len(runs[0].claims), "ambiguous_count": len(runs[0].ambiguous)}
                elif stage == "claims.verify.minicheck":
                    from ..claims.decompose import Claim
                    from ..claims.verify import ClaimVerifier
                    if verifier is None:
                        verifier = ClaimVerifier()
                    claims, truth = [], []
                    for expected in gold["claims"]:
                        found = [(e, e.text.find(expected["quote"])) for e in state["tree"].elements if expected["quote"] in e.text]
                        if not found:
                            raise ValueError("Gold claim quote missing from parsed source")
                        element, start = found[0]
                        claims.append(Claim(text=expected["text"], quote=expected["quote"], element_id=element.id,
                            byte_start=len(element.text[:start].encode()), byte_end=len(element.text[:start + len(expected["quote"])].encode())))
                        truth.append(expected["supported"])
                    predictions = verifier.verify(state["tree"], claims)
                    row["metrics"] = {"gold_agreement": sum(c.verified == g for c, g in zip(predictions, truth)) / len(truth)}
                elif stage == "resolve.link":
                    from ..resolve import resolve_mentions
                    if entity_gold is None:
                        raise RuntimeError("Supply --entity-gold with tenant golden entity IDs for resolution evaluation")
                    mentions = resolve_mentions([s.to_dict() for s in state["spans"].spans], tenant=args.tenant, client=schema)
                    truth = entity_gold["documents"][record["id"]]
                    expected = [(x["field_key"], x["text"], x["entity_id"]) for x in truth]
                    actual = [(x["field_key"], x["text"], x.get("entity_id")) for x in mentions]
                    row["metrics"] = {"resolution_accuracy": f1(actual, expected)["f1"]}
                if not row["metrics"] or any(not isinstance(value, (int, float)) or
                    isinstance(value, bool) or not math.isfinite(value) for value in row["metrics"].values()):
                    raise ValueError("Stage did not produce finite measured metrics")
                row["status"] = "measured"
            except Exception as error:
                row["error"] = {"type": type(error).__name__, "message": str(error)}
            rows.append(row)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--all", action="store_true")
    mode.add_argument("--validate-corpus", action="store_true")
    mode.add_argument("--bootstrap-schema", action="store_true", help="Declare benchmark types and create/read back benchmark entities in the authenticated tenant")
    parser.add_argument("--corpus", type=Path, default=Path(__file__).parent / "corpus")
    parser.add_argument("--tenant", default=os.environ.get("THEOREM_LIVE_TENANT"))
    parser.add_argument("--object-type", default=os.environ.get("THEOREM_LIVE_OBJECT_TYPE", OBJECT_TYPE))
    parser.add_argument("--mineru-dir", type=Path, default=os.environ.get("THEOREM_EXTRACTION_MINERU_DIR"))
    parser.add_argument("--entity-gold", type=Path, default=os.environ.get("THEOREM_EXTRACTION_ENTITY_GOLD"))
    parser.add_argument("--tracking-uri", default=os.environ.get("MLFLOW_TRACKING_URI"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/extraction-bench"))
    args = parser.parse_args()
    if args.entity_gold is None:
        default_gold = args.output / "entity-gold.json"
        if default_gold.exists() or args.bootstrap_schema:
            args.entity_gold = default_gold
    if args.validate_corpus:
        records = load_corpus(args.corpus)
        print(json.dumps({"documents": len(records), "corpus_class": "authored_fixture",
                          "categories": sorted({r["category"] for r in records}),
                          "claim": "Artifact digests only; no model quality evidence"}))
        return 0
    if args.bootstrap_schema:
        if args.object_type != OBJECT_TYPE:
            parser.error("--bootstrap-schema owns only ExtractionBenchLease; select its default object type")
        result = bootstrap(args.corpus, load_corpus(args.corpus), tenant=args.tenant, output=args.entity_gold)
        print(json.dumps({"tenant": result["tenant"], "object_type": result["object_type"],
                          "entity_count": len(result["entities"]), "entity_gold": str(args.entity_gold)}))
        return 0
    if not args.tracking_uri:
        from theorem_ml.executors.registry import extraction_tracking_uri
        args.tracking_uri = extraction_tracking_uri()
    rows = run(args)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "results.jsonl").write_text("".join(json.dumps(r, allow_nan=False) + "\n" for r in rows))
    (args.output / "results.md").write_text(markdown_projection(rows))
    acceptance = evaluate(rows)
    (args.output / "acceptance.json").write_text(json.dumps(acceptance, indent=2) + "\n")
    with (args.output / "results.md").open("a") as projection:
        projection.write(f"\nAcceptance: **{acceptance['status']}**, policy `{acceptance['policy_version']}`.\n")
        for check in acceptance["checks"]:
            projection.write(f"- {check['name']}: {check['status']}\n")
    run_ids = publish(rows, tenant=args.tenant, tracking_uri=args.tracking_uri, output=args.output, acceptance=acceptance)
    (args.output / "mlflow-runs.json").write_text(json.dumps(run_ids, indent=2) + "\n")
    missing = sum(r["status"] != "measured" for r in rows)
    print(json.dumps({"documents": 100, "rows": len(rows), "missing_evidence": missing,
                      "acceptance": acceptance["status"], "failed_quality": acceptance["failed_quality"], "mlflow_runs": run_ids}))
    return 1 if missing or acceptance["status"] != "passed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
