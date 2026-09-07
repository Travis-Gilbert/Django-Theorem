"""Schema and UTF-8 contracts; model doubles are structural fixtures only."""

import pytest

from theorem_ml.extract.elements import DocElement, ElementError, build_tree
from theorem_ml.extract.spans.gliner2 import SpanExtractor, normalize_span
from theorem_ml.extract.spans.labels import LabelSpec, compile_labels, labels_from_fields


def relation(key="landlord"):
    return {"key": key, "label": key.title(), "description": "The person signing the lease",
            "field_type": {"kind": "relation", "target_object_type_id": "Person", "cardinality": "one"}}


class FixtureSchemaClient:
    def __init__(self):
        self.fields = [relation()]
        self.calls = []

    def effective_fields(self, tenant, object_type):
        self.calls.append((tenant, object_type))
        return {"fields": self.fields, "name_singular": object_type, "derive_claims": False}


def test_next_run_observes_new_declared_field_without_cache():
    client = FixtureSchemaClient()
    before = compile_labels(client, "tenant-a", ["Lease"])
    client.fields.append({"key": "deadline", "label": "Deadline", "field_type": {"kind": "date"}, "span": True})
    after = compile_labels(client, "tenant-a", ["Lease"])
    assert [x.label for x in before.labels] == ["Lease.landlord"]
    assert [x.label for x in after.labels] == ["Lease.landlord", "Lease.deadline"]
    assert client.calls == [("tenant-a", "Lease"), ("tenant-a", "Lease")]


def test_enum_classification_and_entity_labels_share_schema():
    fields = [relation(), {"key": "status", "label": "Status", "field_type": {"kind": "enum_many", "variants": ["signed", "expired"]}}]
    schema = labels_from_fields("Lease", fields)
    assert schema.labels[0].target_object_type_id == "Person"
    assert schema.classifications == {"Lease.status": {"labels": ["signed", "expired"], "multi_label": True}}


def test_unicode_offsets_address_exact_occurrence_not_first_string_match():
    e = DocElement("e", "Paragraph", "Élodie met 租金 then 租金")
    label = LabelSpec("Lease.subject", "Subject", "Lease", "subject", "Entity")
    start = e.text.rindex("租金")
    span = normalize_span(e, label, {"text": "租金", "start": start, "end": start + 2, "confidence": .9})
    assert span.byte_start == len(e.text[:start].encode())
    assert span.byte_end - span.byte_start == 6
    assert e.text.encode()[span.byte_start:span.byte_end].decode() == "租金"
    assert span.byte_start > start


@pytest.mark.parametrize("value", [
    {"text": "invented", "start": 0, "end": 3, "confidence": .9},
    {"text": "abc", "start": -1, "end": 3, "confidence": .9},
    {"text": "abc", "start": 0, "end": 3, "confidence": float("nan")},
    {"text": "abc", "start": False, "end": 3, "confidence": .9},
    {"text": "abc", "start": 0, "end": 3, "confidence": True},
])
def test_unrecoverable_or_invalid_model_spans_are_refused(value):
    with pytest.raises(ElementError):
        normalize_span(DocElement("e", "Paragraph", "abc"), LabelSpec("T.f", "field", "T", "f"), value)


def test_composed_inference_runs_once_per_eligible_element_and_only_pends_relations():
    class FixtureModel:
        def __init__(self):
            self.calls = []
        def create_schema(self):
            return self
        def entities(self, labels):
            assert labels == {"Lease.landlord": "The person signing the lease"}
            return self
        def extract(self, text, schema, **options):
            self.calls.append(text)
            assert options == {"include_spans": True, "include_confidence": True}
            return {"entities": {"Lease.landlord": [{"text": "Ada", "start": 0, "end": 3, "confidence": .99}]}}
    tree = build_tree("s", [DocElement("h", "Title", "Title"), DocElement("p", "Paragraph", "Ada signed.")])
    extractor = SpanExtractor(FixtureSchemaClient())
    extractor._model = FixtureModel()
    result = extractor.extract(tree, "tenant-a", ["Lease"])
    assert extractor._model.calls == ["Ada signed."]
    assert result.pending_relations[0].to_dict() == {"field_key": "landlord", "name": "Ada", "target_object_type_id": "Person", "candidates": []}
    assert result.spans[0].global_span(tree)["span"]["byte_start"] == tree.elements[1].byte_start
