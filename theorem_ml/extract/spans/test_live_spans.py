"""Real GLiNER2 weights and schema MCP; absence is an explicit live-proof gap."""

import os
import pytest

from theorem_ml.extract.parse.router import parse_text
from theorem_ml.extract.spans.gliner2 import SpanExtractor


@pytest.mark.skipif(os.environ.get("THEOREM_EXTRACTION_LIVE") != "1", reason="requires pinned GLiNER2 weights and authenticated schema MCP")
def test_live_gliner_spans_slice_unicode_evidence():
    text = os.environ["THEOREM_LIVE_SPAN_TEXT"]
    tree = parse_text(text, "live-span-source").tree
    result = SpanExtractor().extract(tree, os.environ["THEOREM_LIVE_TENANT"], [os.environ["THEOREM_LIVE_OBJECT_TYPE"]])
    assert result.spans, "Live fixture must contain a declared entity span"
    for span in result.spans:
        assert tree.get(span.element_id).text.encode()[span.byte_start:span.byte_end].decode() == span.text
