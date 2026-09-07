"""Pinned GLiNER2 Large inference with verified character-to-byte conversion."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from contextlib import nullcontext
import math

from ..elements import DocElement, DocElementTree, ElementError
from .labels import LabelSpec, SchemaClient, compile_labels


MODEL_ID = "fastino/gliner2-large-v1"
MODEL_REVISION = "6a498b5a28ec3908bbc5277aeb47d22bcfc02f33"
SPAN_KINDS = {"Paragraph", "ListItem", "TableCell", "Caption", "Footnote"}


@dataclass
class PendingRelation:
    field_key: str
    name: str
    target_object_type_id: str
    candidates: list[tuple[str, float]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ExtractedSpan:
    element_id: str
    object_type: str
    field_key: str
    label: str
    text: str
    byte_start: int
    byte_end: int
    confidence: float
    target_object_type_id: str | None = None
    executor: str = f"spans.gliner2:{MODEL_ID}@{MODEL_REVISION}"

    def to_dict(self) -> dict:
        return asdict(self)

    def global_span(self, tree: DocElementTree) -> dict:
        element = tree.get(self.element_id)
        return {"label": self.label, "span": {
            "byte_start": element.byte_start + self.byte_start,
            "byte_end": element.byte_start + self.byte_end,
            "page": element.page, "bbox": element.bbox}}


@dataclass
class SpanResult:
    spans: list[ExtractedSpan]
    pending_relations: list[PendingRelation]
    classifications: dict[str, dict]


def normalize_span(element: DocElement, label: LabelSpec, value: dict) -> ExtractedSpan:
    start, end = value["start"], value["end"]
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(element.text):
        raise ElementError("GLiNER2 returned an invalid character range")
    text = value["text"]
    if element.text[start:end] != text:
        raise ElementError("GLiNER2 span does not match the element verbatim")
    byte_start = len(element.text[:start].encode("utf-8"))
    byte_end = len(element.text[:end].encode("utf-8"))
    if element.text.encode()[byte_start:byte_end] != text.encode():
        raise ElementError("GLiNER2 UTF-8 span conversion failed")
    if isinstance(value["confidence"], bool):
        raise ElementError("GLiNER2 returned an invalid confidence")
    confidence = float(value["confidence"])
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ElementError("GLiNER2 returned an invalid confidence")
    return ExtractedSpan(element.id, label.object_type, label.field_key, label.label,
                         text, byte_start, byte_end, confidence, label.target_object_type_id)


class SpanExtractor:
    def __init__(self, schema_client: SchemaClient | None = None, *, observer=None) -> None:
        self.schema_client = schema_client or SchemaClient()
        self._model = None
        self.observer = observer

    def _load(self):
        if self._model is None:
            from gliner2 import GLiNER2
            from huggingface_hub import snapshot_download

            checkpoint = snapshot_download(repo_id=MODEL_ID, revision=MODEL_REVISION)
            self._model = GLiNER2.from_pretrained(checkpoint)
            self._model.eval()
        return self._model

    def extract(self, tree: DocElementTree, tenant: str, object_types: list[str]) -> SpanResult:
        tree.validate()
        labels = compile_labels(self.schema_client, tenant, object_types)
        if (not labels.labels and not labels.classifications) or not any(e.kind in SPAN_KINDS for e in tree.elements):
            return SpanResult([], [], {})
        context = self.observer("spans.gliner2", {"document": tree.to_dict(),
            "object_types": object_types, "model": MODEL_ID, "revision": MODEL_REVISION}) if self.observer else nullcontext()
        with context:
            return self._extract(tree, labels)

    def _extract(self, tree, labels):
        model = self._load()
        schema = model.create_schema()
        if labels.labels:
            schema = schema.entities({s.label: s.description for s in labels.labels})
        for name, config in labels.classifications.items():
            schema = schema.classification(name, config["labels"], multi_label=config["multi_label"])
        specifications = {s.label: s for s in labels.labels}
        spans, pending, classifications = [], [], {}
        for element in tree.elements:
            if element.kind not in SPAN_KINDS:
                continue
            result = model.extract(element.text, schema, include_spans=True, include_confidence=True)
            for label, values in result.get("entities", {}).items():
                if label not in specifications:
                    raise ElementError(f"GLiNER2 emitted undeclared label: {label}")
                for value in values:
                    span = normalize_span(element, specifications[label], value)
                    spans.append(span)
                    if span.target_object_type_id is not None:
                        pending.append(PendingRelation(span.field_key, span.text, span.target_object_type_id))
            classifications[element.id] = {k: result[k] for k in labels.classifications if k in result}
        return SpanResult(spans, pending, classifications)
