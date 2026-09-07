"""Routing contracts. These fixtures are not Paddle/Docling accuracy evidence."""

from theorem_ml.extract.elements import DocElement, build_tree
from theorem_ml.extract.parse.router import PageFeatures, PageRoute, ParserReceipt, merge_pages, parse_text, source_metadata


def test_mixed_forty_page_routes_are_per_page():
    pages = [PageFeatures(i, f"Native text {i}", i > 35, 30 < i <= 35) for i in range(1, 41)]
    receipt = ParserReceipt("parse.router", "2.0", [PageRoute(p.page, p.route, "fixture", p.reason) for p in pages], "fixture")
    routes = receipt.to_dict()["coerced"]["routes"]
    assert len(routes) == 40
    assert sum(r["parser"] == "parse.docling" for r in routes) == 30
    assert sum(r["parser"] == "parse.paddle" for r in routes) == 10
    assert [r["page"] for r in routes] == list(range(1, 41))


def test_unextractable_text_and_scans_route_to_paddle():
    assert PageFeatures(1, "", False, False).route == "parse.paddle"
    assert PageFeatures(1, "broken � text", False, False).route == "parse.paddle"
    assert PageFeatures(1, "Native café", False, False).route == "parse.docling"


def test_page_replacement_rebases_spans_and_removes_replaced_ancestors():
    native = build_tree("s", [DocElement("h", "SectionHeader", "Native", page=1),
                               DocElement("p", "Paragraph", "Résumé", page=2, parent="h")])
    paddle = build_tree("s", [DocElement("new", "Paragraph", "Scanned text", page=1)])
    combined = merge_pages("s", native, {1: paddle}, 2)
    assert [e.id for e in combined.elements] == ["new", "p"]
    assert combined.elements[1].parent is None
    assert combined.elements[1].byte_start == len("Scanned text\n".encode())
    combined.validate()


def test_native_text_is_verbatim_including_whitespace():
    result = parse_text("\n café\t租金 \n", "capture")
    assert result.tree.text == "\n café\t租金 \n"
    assert result.receipt.parser == "parse.native_text"
    assert result.tree.elements[0].byte_end == len(result.tree.text.encode())


def test_email_message_date_comes_from_source_header(tmp_path):
    from datetime import UTC, datetime
    from theorem_ml.extract.temporal import source_time
    path = tmp_path / "source.eml"
    path.write_bytes(b"From: author@example.invalid\r\nDate: Mon, 01 Jun 2026 09:30:00 -0400\r\n\r\nBody")
    metadata = source_metadata(path)
    assert source_time({"document_date": "2026-09-05T00:00:00+00:00", **metadata}, datetime.now(UTC)) == "2026-06-01T13:30:00+00:00"
