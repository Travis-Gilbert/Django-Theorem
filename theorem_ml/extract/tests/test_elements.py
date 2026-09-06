"""Structural fixtures test normalization, not model or parser accuracy."""

import pytest

from theorem_ml.extract.elements import (
    DocElement, DocElementTree, ElementError, build_tree, from_docling, from_paddle,
)


def docling_table_fixture():
    return {"body": {"self_ref": "#/body", "children": [{"$ref": "#/texts/0"}]},
        "texts": [{"self_ref": "#/texts/0", "label": "section_header", "level": 2,
                   "text": "Résumé", "children": [{"$ref": "#/tables/0"}],
                   "prov": [{"page_no": 1, "bbox": {"l": 0, "t": 1, "r": 2, "b": 3}}]}],
        "tables": [{"self_ref": "#/tables/0", "label": "table", "children": [],
                    "prov": [{"page_no": 1}], "data": {"num_rows": 2, "num_cols": 2,
                    "table_cells": [
                        {"text": "租金", "start_row_offset_idx": 0, "start_col_offset_idx": 0, "row_span": 1, "col_span": 2},
                        {"text": "€1450", "start_row_offset_idx": 1, "start_col_offset_idx": 0},
                        {"text": "monthly", "start_row_offset_idx": 1, "start_col_offset_idx": 1},
                    ]}}]}


def test_docling_preserves_structure_merged_cells_and_utf8_spans():
    tree = from_docling(docling_table_fixture(), "source")
    header, table, *cells = tree.elements
    assert header.attributes["level"] == 2
    assert table.parent == header.id
    assert [c.parent for c in cells] == [table.id] * 3
    assert table.attributes["rows"] == [["租金", ""], ["€1450", "monthly"]]
    assert table.attributes["merged_cells"] == [{"row": 0, "column": 0, "row_span": 1, "col_span": 2}]
    assert cells[0].byte_end - cells[0].byte_start == 6
    assert DocElementTree.from_dict(tree.to_dict()) == tree
    assert [e.order for e in tree.elements] == list(range(len(tree.elements)))
    assert table.attributes["table_data"] == docling_table_fixture()["tables"][0]["data"]
    for element in tree.elements:
        assert tree.text.encode()[element.byte_start:element.byte_end].decode() == element.text


def test_paddle_html_and_docling_table_normalize_equivalently():
    data = {"res": {"page_index": 0, "parsing_res_list": [
        {"block_label": "paragraph_title", "block_content": "Résumé", "level": 2,
         "block_order": 0, "block_bbox": [0, 1, 2, 3]},
        {"block_label": "table", "block_order": 1, "block_content":
         '<table><tr><th colspan="2">租金</th></tr><tr><td>€1450</td><td>monthly</td></tr></table>'},
    ]}}
    paddle = from_paddle(data, "source")
    docling = from_docling(docling_table_fixture(), "source")
    assert [(e.kind, e.text) for e in paddle.elements] == [(e.kind, e.text) for e in docling.elements]
    assert paddle.elements[1].attributes["rows"] == docling.elements[1].attributes["rows"]
    assert paddle.elements[1].attributes["merged_cells"] == docling.elements[1].attributes["merged_cells"]


def test_duplicate_identity_or_invalid_span_cannot_enter_tree():
    element = DocElement("e", "Paragraph", "café")
    with pytest.raises(ElementError, match="Duplicate"):
        build_tree("s", [element, element])
    tree = build_tree("s", [element])
    tree.elements[0].byte_end -= 1
    with pytest.raises(ElementError, match="mismatch"):
        tree.validate()


def test_dangling_parser_references_are_refused_not_silently_dropped():
    with pytest.raises(ElementError, match="Dangling"):
        from_docling({"body": {"self_ref": "#/body", "children": [{"$ref": "#/texts/missing"}]}}, "s")


def test_unknown_paddle_labels_are_refused_instead_of_erasing_structure():
    with pytest.raises(ElementError, match="Unsupported"):
        from_paddle({"parsing_res_list": [{"block_label": "future-kind", "block_content": "text"}]}, "s")


def test_flat_docling_headers_become_section_subtrees():
    labels = [("section_header", "Heading", 1), ("text", "Paragraph", None),
              ("section_header", "Nested", 2), ("text", "Nested paragraph", None)]
    data = {"body": {"self_ref": "#/body", "children": [{"$ref": f"#/texts/{i}"} for i in range(4)]},
            "texts": [{"self_ref": f"#/texts/{i}", "label": label, "text": text,
                       **({"level": level} if level is not None else {})}
                      for i, (label, text, level) in enumerate(labels)]}
    tree = from_docling(data, "s")
    assert tree.elements[1].parent == tree.elements[0].id
    assert tree.elements[2].parent == tree.elements[0].id
    assert tree.elements[3].parent == tree.elements[2].id


def test_real_docling_document_export_retains_table_and_caption_ownership():
    from docling_core.types.doc import DoclingDocument, DocItemLabel, TableData, TableCell

    document = DoclingDocument(name="structural-fixture")
    heading = document.add_heading("Rent", level=1)
    caption = document.add_text(DocItemLabel.CAPTION, "Rent schedule")
    document.add_table(parent=heading, caption=caption, data=TableData(num_rows=1, num_cols=2,
        table_cells=[TableCell(text="€1450", row_span=1, col_span=2,
            start_row_offset_idx=0, end_row_offset_idx=1,
            start_col_offset_idx=0, end_col_offset_idx=2)]))
    tree = from_docling(document, "structural-fixture")
    table = next(e for e in tree.elements if e.kind == "Table")
    caption_element = next(e for e in tree.elements if e.kind == "Caption")
    assert caption_element.parent == table.id
    assert table.attributes["rows"] == [["€1450", ""]]
    assert table.attributes["merged_cells"] == [{"row": 0, "column": 0, "row_span": 1, "col_span": 2}]
    tree.validate()
