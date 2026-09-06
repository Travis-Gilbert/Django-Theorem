"""Real local MLflow/database persistence with explicitly named model fixtures."""
from dataclasses import replace

import pytest
from mlflow import MlflowClient

from apps.extraction.services.capture_admission import AdmissionPipeline, capture, schedule
from apps.tenancy.models import Tenant
from theorem_ml.extract.claims.decompose import Claim, Decomposition
from theorem_ml.extract.elements import DocElement, build_tree
from theorem_ml.extract.parse.router import ParserRouter
from theorem_ml.extract.spans.gliner2 import SpanExtractor


class FixtureSchema:
    evidence_class = "fixture_execution"

    def effective_fields(self, tenant, object_type):
        return {"name_singular": "Lease", "derive_claims": True, "fields": [{"key": "landlord",
            "field_type": {"kind": "relation", "target_object_type_id": "Person", "cardinality": "one"}}]}

    def call(self, tenant, action, **kwargs):
        if action == "extract_fields":
            return {"properties": {}, "attempts": []}
        if action == "link_or_pend":
            return {"status": "linked", "value": "person:fixture-ada"}
        assert action == "publish"
        return {"published": True}


class FixtureGlinerModel:
    def create_schema(self):
        return self

    def entities(self, labels):
        return self

    def extract(self, text, schema, **kwargs):
        return {"entities": {"Lease.landlord": [{"text": "Ada", "start": 0, "end": 3, "confidence": .9}]}}


class FixtureDecomposer:
    def decompose(self, tree, **kwargs):
        element = tree.elements[0]
        return Decomposition([Claim(element.text, element.text, element.id, 0, len(element.text.encode()))])


class FixtureVerifier:
    evidence_class = "fixture_execution"

    def __init__(self, cutoff=.5):
        self.cutoff = cutoff

    def verify(self, tree, claims):
        return [replace(claim, verified=True, score=.9) for claim in claims]


@pytest.mark.django_db
def test_pipeline_persists_only_executed_stage_run_ids(tmp_path, monkeypatch):
    uri = "sqlite:///" + str(tmp_path / "mlflow.db")
    monkeypatch.setenv("MLFLOW_TRACKING_URI", uri)
    tenant = Tenant.objects.create(slug="mlflow-fixture", display_name="Fixture")
    schema = FixtureSchema()
    spans = SpanExtractor(schema)
    spans._model = FixtureGlinerModel()
    spans.evidence_class = "fixture_execution"
    pipeline = AdmissionPipeline(schema=schema, spans=spans, decomposer=FixtureDecomposer(),
        verifier=FixtureVerifier(), scorer=lambda state, tenant: {"status": "fixture_only", "score": None})
    artifact, _ = capture(tenant, text="Ada signed.")
    run, _ = schedule(artifact, ["Lease"])
    pipeline.execute(run.id)
    run.refresh_from_db()
    refs = run.output["executor_runs"]
    assert [r["name"] for r in refs] == ["spans.gliner2", "resolve.link", "claims.verify.minicheck"]
    client = MlflowClient(tracking_uri=uri)
    for ref in refs:
        observed = client.get_run(ref["mlflow_run_id"])
        assert observed.info.status == ref["status"] == "FINISHED"
        assert ref["fitness"] is None
        assert observed.data.tags["theorem.tenant"] == tenant.slug
        assert observed.data.tags["theorem.evidence"] == "fixture_execution"
        assert set(observed.data.metrics) == {"elapsed_seconds"}
    pipeline.execute(run.id)
    run.refresh_from_db()
    assert run.output["executor_runs"] == refs


def test_parser_observes_paddle_image_and_skips_native_code(tmp_path):
    from theorem_ml.executors.registry import observe_execution
    state = {}
    uri = "sqlite:///" + str(tmp_path / "parse.db")

    class FixturePaddle:
        parser_id, version = "parse.paddle", "fixture"

        def parse(self, path, source_id, page):
            return build_tree(source_id, [DocElement("p", "Paragraph", "Fixture text", page=page)])

    router = ParserRouter(paddle=FixturePaddle(), observer=lambda name, payload:
        observe_execution(name, payload, tenant="fixture", state=state, tracking_uri=uri,
                          evidence_class="fixture_execution"))
    code = tmp_path / "source.py"
    code.write_text("print('native')")
    router.parse(code, "code")
    assert state == {}
    image = tmp_path / "source.png"
    image.write_bytes(b"explicit image adapter fixture")
    result = router.parse(image, "image")
    assert result.receipt.routes[0].parser == "parse.paddle"
    assert len(state["executor_runs"]) == 1
    assert state["executor_runs"][0]["name"] == "parse.paddle"


def test_span_extractor_without_eligible_elements_never_opens_model_observation():
    spans = SpanExtractor(FixtureSchema(), observer=lambda *args: pytest.fail("No model executed"))
    spans._load = lambda: pytest.fail("No eligible text should load model")
    tree = build_tree("code", [DocElement("c", "Code", "print('hello')")])
    assert spans.extract(tree, "fixture", ["Lease"]).spans == []


@pytest.mark.django_db
def test_scheduled_escalation_records_actual_verifier_call(tmp_path, monkeypatch, settings):
    from apps.extraction import document_tasks
    from theorem_ml.extract.parse.router import parse_text
    uri = "sqlite:///" + str(tmp_path / "batch.db")
    monkeypatch.setenv("MLFLOW_TRACKING_URI", uri)
    settings.EXTRACTION_FRONTIER_BINDINGS = []
    tenant = Tenant.objects.create(slug="batch-fixture", display_name="Fixture")
    artifact, _ = capture(tenant, text="Ada signed.")
    parent, _ = schedule(artifact, ["Lease"])
    tree = parse_text(artifact.source_text, "fixture").tree
    parent.output = {"document": tree.to_dict(), "observed_at": "2026-09-05T00:00:00+00:00",
                     "records": {}, "entities": [], "pending_relations": [], "claims": []}
    parent.ambiguity = [{"element_id": tree.elements[0].id, "quote": tree.text,
                         "reason": "finite_verb_without_claim"}]
    parent.save()
    batch, _ = schedule(artifact, ["Lease"], kind="claim_escalation", parent=parent)

    class FixtureBatch:
        def __init__(self, bindings):
            pass

        def decompose(self, tree, ambiguity, run_kind):
            assert run_kind == "claim_escalation"
            return FixtureDecomposer().decompose(tree).claims

    monkeypatch.setattr(document_tasks, "BatchDecomposer", FixtureBatch)
    monkeypatch.setattr(document_tasks, "ClaimVerifier", FixtureVerifier)
    monkeypatch.setattr(document_tasks, "AdmissionPipeline", lambda: AdmissionPipeline(schema=FixtureSchema()))
    assert document_tasks.escalate_claims(batch.id)["claims"] == 1
    batch.refresh_from_db()
    refs = batch.output["executor_runs"]
    assert len(refs) == 1 and refs[0]["name"] == "claims.verify.minicheck"
    assert refs[0]["fitness"] is None
    observed = MlflowClient(tracking_uri=uri).get_run(refs[0]["mlflow_run_id"])
    assert observed.info.status == "FINISHED"
    assert observed.data.tags["theorem.evidence"] == "fixture_execution"
