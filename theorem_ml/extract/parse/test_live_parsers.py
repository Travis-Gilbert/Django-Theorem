"""Real parser/model parity. Run with THEOREM_EXTRACTION_LIVE=1 and fixtures.

THEOREM_LIVE_DOCX and THEOREM_LIVE_RENDERED_PAGE may override the checked-in
LibreOffice-rendered merged-table pair. No synthetic replay substitutes here.
"""

import os
from pathlib import Path

import pytest

from theorem_ml.extract.parse.docling import DoclingParser
from theorem_ml.extract.parse.paddle import PaddleParser


@pytest.mark.skipif(os.environ.get("THEOREM_EXTRACTION_LIVE") != "1", reason="requires real parser models and paired source documents")
def test_live_docling_paddle_text_and_table_parity():
    corpus = Path(__file__).parents[1] / "bench/corpus"
    native = DoclingParser().parse(Path(os.environ.get("THEOREM_LIVE_DOCX", corpus / "docx-01.docx")), "live-source")
    page = PaddleParser().parse(Path(os.environ.get("THEOREM_LIVE_RENDERED_PAGE", corpus / "docx-01.png")), "live-source")
    assert [(e.kind, e.text) for e in native.elements] == [(e.kind, e.text) for e in page.elements]
    assert [e.attributes["rows"] for e in native.elements if e.kind == "Table"] == [e.attributes["rows"] for e in page.elements if e.kind == "Table"]
    assert [e.attributes["merged_cells"] for e in native.elements if e.kind == "Table"] == [e.attributes["merged_cells"] for e in page.elements if e.kind == "Table"]
