"""Real Office-container and rendered-PDF proof; no synthetic parser output."""
import json
import os
from pathlib import Path

import pytest

from theorem_ml.extract.bench.__main__ import load_corpus
from theorem_ml.extract.bench.score import score_parse
from theorem_ml.extract.parse.docling import DoclingParser

CORPUS = Path(__file__).parents[1] / "bench/corpus"


def test_real_docx_container_and_libreoffice_render_preserve_both_merge_axes():
    import pypdfium2 as pdfium
    records = load_corpus(CORPUS)
    row = next(r for r in records if r["id"] == "docx-01")
    assert row["page_image_provenance"] == "libreoffice_docx_pdfium_rendered"
    receipt = json.loads((CORPUS / row["renderer_receipt"]).read_text())
    assert receipt["renderer"] == "LibreOffice" and receipt["page_count"] == 1
    gold = json.loads((CORPUS / row["gold"]).read_text())
    assert any(c["row_span"] > 1 for c in gold["merged_cells"][0])
    assert any(c["col_span"] > 1 for c in gold["merged_cells"][0])
    tree = DoclingParser().parse(CORPUS / row["source"], "real-docx")
    metrics = score_parse(tree, gold)
    assert all(value == 1. for value in metrics.values()), metrics
    assert [e.order for e in tree.elements] == list(range(len(tree.elements)))
    pdf = pdfium.PdfDocument(str(CORPUS / row["comparison_pdf"]))
    try:
        assert len(pdf) == 1
        page = pdf[0]
        try:
            textpage = page.get_textpage()
            try:
                text = textpage.get_text_range()
                assert all(value in text for value in gold["texts"])
            finally:
                textpage.close()
        finally:
            page.close()
    finally:
        pdf.close()


@pytest.mark.skipif(os.environ.get("THEOREM_EXTRACTION_LIVE_DOCLING") != "1",
                    reason="requires actual Docling PDF layout/table weights")
def test_live_docling_docx_and_its_real_rendered_pdf_match_authored_gold():
    gold = json.loads((CORPUS / "docx-01.gold.json").read_text())
    parser = DoclingParser()
    for extension in ("docx", "pdf"):
        tree = parser.parse(CORPUS / ("docx-01." + extension), "same-source")
        metrics = score_parse(tree, gold)
        assert metrics["exact_text_f1"] >= .95, (extension, metrics)
        for key in ("table_structure_f1", "merged_cells_f1", "heading_hierarchy_f1", "span_recoverability"):
            assert metrics[key] == 1., (extension, metrics)
