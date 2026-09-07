"""Lossless element normalization; UTF-8 spans address the tree's text buffer.

The normalized buffer is a representation of extracted text, not byte offsets
into a binary PDF or ZIP container. Parser-native provenance is retained in
attributes, including all Docling provenance regions and table cell extents.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from hashlib import sha256
from typing import Any, Iterable, Mapping


KINDS = frozenset({"Title", "SectionHeader", "Paragraph", "ListItem", "Table",
                   "TableCell", "Figure", "Formula", "Code", "Header", "Footer",
                   "PageBreak", "Footnote", "Caption"})


class ElementError(ValueError):
    """Parser output cannot be represented without inventing source evidence."""


@dataclass
class DocElement:
    id: str
    kind: str
    text: str
    byte_start: int = 0
    byte_end: int = 0
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    parent: str | None = None
    order: int = 0
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DocElement":
        values = dict(value)
        if values.get("bbox") is not None:
            values["bbox"] = tuple(values["bbox"])
        return cls(**values)


@dataclass
class DocElementTree:
    source_id: str
    text: str
    elements: list[DocElement]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DocElementTree":
        tree = cls(str(value["source_id"]), value["text"],
                   [DocElement.from_dict(x) for x in value["elements"]])
        tree.validate()
        return tree

    def get(self, element_id: str) -> DocElement:
        return next(e for e in self.elements if e.id == element_id)

    def validate(self) -> None:
        encoded = self.text.encode("utf-8")
        ids = {e.id for e in self.elements}
        if len(ids) != len(self.elements):
            raise ElementError("Duplicate element identity")
        parents = {e.id: e.parent for e in self.elements}
        for e in self.elements:
            if e.kind not in KINDS:
                raise ElementError(f"Unknown element kind: {e.kind}")
            if not 0 <= e.byte_start <= e.byte_end <= len(encoded):
                raise ElementError(f"Invalid byte range for {e.id}")
            if encoded[e.byte_start:e.byte_end] != e.text.encode("utf-8"):
                raise ElementError(f"Text/span mismatch for {e.id}")
            if e.page is not None and e.page < 1:
                raise ElementError("Page numbers are one-based")
            if e.parent is not None and e.parent not in ids:
                raise ElementError(f"Missing parent for {e.id}")
            visited = {e.id}
            parent = e.parent
            while parent is not None:
                if parent in visited:
                    raise ElementError("Cyclic element hierarchy")
                visited.add(parent)
                parent = parents[parent]


def element_id(source_id: str, address: str) -> str:
    return sha256(f"{source_id}\0{address}".encode()).hexdigest()


def build_tree(source_id: str, elements: Iterable[DocElement]) -> DocElementTree:
    """Rebase copied elements into one deterministic buffer, without text edits."""
    result: list[DocElement] = []
    chunks: list[str] = []
    position = 0
    for order, e in enumerate(elements):
        if chunks:
            chunks.append("\n")
            position += 1
        raw = e.text.encode("utf-8")
        result.append(replace(e, order=order, byte_start=position, byte_end=position + len(raw)))
        chunks.append(e.text)
        position += len(raw)
    tree = DocElementTree(source_id, "".join(chunks), result)
    tree.validate()
    return tree


def section_hierarchy(elements: list[DocElement]) -> list[DocElement]:
    """Give flat parser headings explicit subtrees, preserving explicit parents."""
    sections: list[tuple[int, str]] = []
    output = []
    for e in elements:
        if e.kind == "SectionHeader":
            level = int(e.attributes.get("level", 1))
            while sections and sections[-1][0] >= level:
                sections.pop()
            if e.parent is None and sections:
                e = replace(e, parent=sections[-1][1])
            sections.append((level, e.id))
        elif e.kind not in {"Title", "Header", "Footer", "PageBreak"} and e.parent is None and sections:
            e = replace(e, parent=sections[-1][1])
        output.append(e)
    return output


DOCLING_LABELS = {
    "title": "Title", "section_header": "SectionHeader", "text": "Paragraph",
    "paragraph": "Paragraph", "list_item": "ListItem", "table": "Table",
    "picture": "Figure", "formula": "Formula", "code": "Code",
    "page_header": "Header", "page_footer": "Footer", "page_break": "PageBreak",
    "footnote": "Footnote", "caption": "Caption", "checkbox_selected": "Paragraph",
    "checkbox_unselected": "Paragraph", "reference": "Paragraph",
}


def _bbox(value: Any) -> tuple[float, float, float, float] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return tuple(float(value[k]) for k in ("l", "t", "r", "b"))
    if len(value) != 4:
        raise ElementError("Expected four bounding-box coordinates")
    return tuple(float(x) for x in value)


def _table_elements(source_id: str, parent: DocElement, cells: list[dict],
                    nrows: int, ncols: int) -> list[DocElement]:
    rows = [["" for _ in range(ncols)] for _ in range(nrows)]
    merged = []
    result = []
    occupied: set[tuple[int, int]] = set()
    for index, cell in enumerate(cells):
        r, c = int(cell["start_row_offset_idx"]), int(cell["start_col_offset_idx"])
        rs, cs = int(cell.get("row_span", 1)), int(cell.get("col_span", 1))
        if min(rs, cs) < 1 or r < 0 or c < 0 or r + rs > nrows or c + cs > ncols:
            raise ElementError("Invalid table cell extent")
        for rr in range(r, r + rs):
            for cc in range(c, c + cs):
                if (rr, cc) in occupied:
                    raise ElementError("Overlapping table cells")
                occupied.add((rr, cc))
        text = str(cell.get("text", ""))
        rows[r][c] = text
        if rs > 1 or cs > 1:
            merged.append({"row": r, "column": c, "row_span": rs, "col_span": cs})
        result.append(DocElement(
            id=element_id(source_id, f"{parent.id}/cell/{index}"), kind="TableCell",
            text=text, page=parent.page, bbox=_bbox(cell.get("bbox")),
            parent=parent.id, order=index, attributes=dict(cell),
        ))
    parent.attributes.update(rows=rows, merged_cells=merged, num_rows=nrows, num_cols=ncols)
    return result


def from_docling(document: Any, source_id: str) -> DocElementTree:
    """Normalize a real DoclingDocument or its lossless export_to_dict result."""
    data = document if isinstance(document, Mapping) else document.export_to_dict()
    references: dict[str, dict] = {}
    for collection in ("texts", "tables", "pictures", "groups"):
        for item in data.get(collection, []):
            references[item["self_ref"]] = item
    for name in ("body", "furniture"):
        if data.get(name):
            references[data[name]["self_ref"]] = data[name]
    output: list[DocElement] = []
    active: set[str] = set()
    emitted: set[str] = set()

    def visit(ref: str, parent: str | None) -> None:
        if ref in active:
            raise ElementError("Docling document contains a cycle")
        if ref in emitted:
            return
        if ref not in references:
            raise ElementError(f"Dangling Docling reference: {ref}")
        active.add(ref)
        item = references[ref]
        label = item.get("label", "")
        kind = DOCLING_LABELS.get(label)
        child_parent = parent
        if kind:
            prov = item.get("prov", [])
            first = prov[0] if prov else {}
            attrs = {k: v for k, v in item.items()
                     if k not in {"text", "children", "parent", "self_ref", "label", "data"}}
            attrs["source_ref"] = ref
            attrs["source_label"] = label
            e = DocElement(element_id(source_id, f"docling/{ref}"), kind,
                           str(item.get("text", "")), page=first.get("page_no"),
                           bbox=_bbox(first.get("bbox")), parent=parent,
                           order=len(output), attributes=attrs)
            if kind == "SectionHeader":
                e.attributes["level"] = int(item.get("level", 1))
            if kind == "Formula":
                e.attributes["latex"] = e.text
            if kind == "Code":
                e.attributes["language"] = item.get("code_language", "unknown")
            if kind == "Figure":
                e.attributes["caption"] = "\n".join(
                    references[x["$ref"]].get("text", "") for x in item.get("captions", [])
                )
            output.append(e)
            child_parent = e.id
            if kind == "Table":
                table = item.get("data", {})
                e.attributes["table_data"] = table
                output.extend(_table_elements(source_id, e, table.get("table_cells", []),
                                              int(table.get("num_rows", 0)),
                                              int(table.get("num_cols", 0))))
        elif not item.get("children") and item.get("text"):
            raise ElementError(f"Unsupported Docling text label: {label}")
        for child in item.get("children", []):
            visit(child["$ref"], child_parent)
        active.remove(ref)
        emitted.add(ref)

    for name in ("body", "furniture"):
        if data.get(name):
            visit(data[name]["self_ref"], None)
    # Captions may be linked through captions rather than children.
    for ref, item in references.items():
        if item.get("label") in DOCLING_LABELS and ref not in emitted:
            parent_ref = item.get("parent", {}).get("$ref")
            parent = element_id(source_id, f"docling/{parent_ref}") if parent_ref in emitted and references[parent_ref].get("label") in DOCLING_LABELS else None
            visit(ref, parent)
    # Docling's caption references are semantic ownership, independently of
    # where the caption was encountered in the body traversal.
    by_id = {e.id: e for e in output}
    for ref, item in references.items():
        owner_id = element_id(source_id, f"docling/{ref}")
        if owner_id in by_id:
            for caption in item.get("captions", []):
                caption_id = element_id(source_id, f"docling/{caption['$ref']}")
                if caption_id in by_id:
                    by_id[caption_id].parent = owner_id
    return build_tree(source_id, section_hierarchy(output))


PADDLE_LABELS = {
    "doc_title": "Title", "paragraph_title": "SectionHeader", "text": "Paragraph",
    "list": "ListItem", "list_item": "ListItem", "table": "Table", "image": "Figure",
    "chart": "Figure", "figure": "Figure", "formula": "Formula", "display_formula": "Formula",
    "inline_formula": "Formula", "code": "Code", "header": "Header", "footer": "Footer",
    "number": "Footer", "footnote": "Footnote", "reference": "Paragraph",
    "figure_title": "Caption", "table_title": "Caption", "table_caption": "Caption",
    "figure_caption": "Caption", "caption": "Caption", "abstract": "Paragraph",
    "content": "Paragraph", "aside_text": "Paragraph", "vision_footnote": "Footnote",
    "seal": "Figure", "algorithm": "Code",
}


def html_table_cells(html: str) -> tuple[list[dict], int, int]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if table is None:
        raise ElementError("Paddle table output omitted structural HTML table")
    cells, occupied = [], set()
    ncols = 0
    rows = [r for r in table.find_all("tr") if r.find_parent("table") is table]
    for r, row in enumerate(rows):
        c = 0
        for cell in row.find_all(["th", "td"], recursive=False):
            while (r, c) in occupied:
                c += 1
            rs, cs = int(cell.get("rowspan", 1)), int(cell.get("colspan", 1))
            for rr in range(r, r + rs):
                for cc in range(c, c + cs):
                    occupied.add((rr, cc))
            cells.append({"text": cell.get_text(), "start_row_offset_idx": r,
                          "start_col_offset_idx": c, "row_span": rs, "col_span": cs,
                          "column_header": cell.name == "th"})
            c += cs
            ncols = max(ncols, c)
    return cells, len(rows), ncols


def from_paddle(result: Any, source_id: str, *, page: int | None = None) -> DocElementTree:
    """Normalize official Paddle result.json, retaining layout order and HTML tables."""
    data = result if isinstance(result, Mapping) else result.json
    if isinstance(data, str):
        import json
        data = json.loads(data)
    data = data.get("res", data)
    page = page if page is not None else int(data.get("page_index") or 0) + 1
    if "parsing_res_list" not in data:
        raise ElementError("Expected official Paddle page parsing_res_list")
    # The official parsing_res_list is already in reading order. block_order
    # excludes furniture and must not be sorted together with array indices.
    blocks = list(enumerate(data["parsing_res_list"]))
    elements: list[DocElement] = []
    sections: list[tuple[int, str]] = []
    for index, block in blocks:
        label = block["block_label"]
        if label not in PADDLE_LABELS:
            raise ElementError(f"Unsupported Paddle block label: {label}")
        kind = PADDLE_LABELS[label]
        attrs = {k: v for k, v in block.items() if k != "block_content"}
        text = str(block.get("block_content", ""))
        if kind == "SectionHeader":
            level = int(block.get("level", 1))
            while sections and sections[-1][0] >= level:
                sections.pop()
            attrs["level"] = level
        parent = sections[-1][1] if sections and kind not in {"Title", "Header", "Footer"} else None
        e = DocElement(element_id(source_id, f"paddle/{page}/{index}"), kind,
                       "" if kind == "Table" else text, page=page,
                       bbox=_bbox(block.get("block_bbox")), parent=parent,
                       order=len(elements), attributes=attrs)
        if kind == "Formula":
            attrs["latex"] = text
        if kind == "Code":
            attrs["language"] = block.get("language", "unknown")
        if kind == "Figure":
            attrs["caption"] = block.get("caption", "")
        elements.append(e)
        if kind == "SectionHeader":
            sections.append((attrs["level"], e.id))
        if kind == "Table":
            attrs["source_html"] = text
            cells, nrows, ncols = html_table_cells(text)
            elements.extend(_table_elements(source_id, e, cells, nrows, ncols))
    owners = {e.attributes["group_id"]: e.id for e in elements
              if e.kind in {"Figure", "Table"} and "group_id" in e.attributes}
    for e in elements:
        if e.kind == "Caption" and e.attributes.get("group_id") in owners:
            e.parent = owners[e.attributes["group_id"]]
    return build_tree(source_id, elements)
