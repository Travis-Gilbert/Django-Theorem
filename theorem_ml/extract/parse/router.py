"""Route each PDF page independently; native document formats stay in Docling."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from contextlib import nullcontext
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from ..elements import DocElement, DocElementTree, build_tree, element_id
from .docling import DoclingParser
from .paddle import PaddleParser


@dataclass(frozen=True)
class PageRoute:
    page: int
    parser: str
    version: str
    reason: str


@dataclass
class ParserReceipt:
    parser: str
    version: str
    routes: list[PageRoute]
    source_sha256: str

    def to_dict(self) -> dict:
        """Existing ParserReceipt wire, with page routes in coerced metadata."""
        return {"field_key": "DocElement", "parser_id": self.parser,
                "raw": {"source_sha256": self.source_sha256},
                "coerced": {"version": self.version,
                            "routes": [asdict(r) for r in self.routes]}}


@dataclass
class ParseResult:
    tree: DocElementTree
    receipt: ParserReceipt
    metadata: dict = field(default_factory=dict)


def source_metadata(path: Path) -> dict:
    """Read source-owned timestamps without trusting caller-supplied metadata."""
    if path.suffix.lower() == ".eml":
        from email import policy
        from email.parser import BytesParser
        message = BytesParser(policy=policy.default).parsebytes(path.read_bytes(), headersonly=True)
        return {"message_date": str(message["Date"])} if message["Date"] else {}
    if path.suffix.lower() in {".docx", ".pptx", ".xlsx"}:
        from zipfile import ZipFile
        from defusedxml.ElementTree import fromstring
        with ZipFile(path) as archive:
            if "docProps/core.xml" not in archive.namelist():
                return {}
            info = archive.getinfo("docProps/core.xml")
            if info.file_size > 1024 * 1024:
                raise ValueError("Document timestamp metadata exceeds size limit")
            root = fromstring(archive.read(info))
            created = root.find("{http://purl.org/dc/terms/}created")
            return {"document_date": created.text} if created is not None and created.text else {}
    return {}


@dataclass(frozen=True)
class PageFeatures:
    page: int
    native_text: str
    has_images: bool
    has_tables: bool

    @property
    def route(self) -> str:
        return "parse.paddle" if self.has_images or self.has_tables or not self.native_text.strip() or "\ufffd" in self.native_text else "parse.docling"

    @property
    def reason(self) -> str:
        if self.has_images:
            return "images"
        if self.has_tables:
            return "tables"
        if not self.native_text.strip() or "\ufffd" in self.native_text:
            return "non_extractable_text"
        return "native_text"


def merge_pages(source_id: str, native: DocElementTree,
                replacements: dict[int, DocElementTree], page_count: int) -> DocElementTree:
    """Keep native hierarchy when its ancestor survives, and rebase every span."""
    output: list[DocElement] = []
    for page in range(1, page_count + 1):
        if page in replacements:
            output.extend(replacements[page].elements)
        else:
            output.extend(e for e in native.elements if e.page == page)
    output.extend(e for e in native.elements if e.page is None)
    kept = {e.id for e in output}
    ancestors = {e.id: e.parent for e in native.elements}
    result = []
    for order, e in enumerate(output):
        parent = e.parent
        while parent is not None and parent not in kept:
            parent = ancestors.get(parent)
        result.append(replace(e, order=order, parent=parent))
    return build_tree(source_id, result)


class ParserRouter:
    def __init__(self, *, docling: DoclingParser | None = None,
                 paddle: PaddleParser | None = None, observer=None) -> None:
        self.docling = docling or DoclingParser()
        self.paddle = paddle or PaddleParser()
        self.observer = observer

    def _paddle(self, path, source_id, page):
        context = self.observer("parse.paddle", {"source_id": source_id, "page": page,
            "image_sha256": sha256(path.read_bytes()).hexdigest()}) if self.observer else nullcontext()
        with context:
            return self.paddle.parse(path, source_id, page=page)

    def parse(self, path: Path, source_id: str) -> ParseResult:
        path = Path(path)
        source_digest = sha256(path.read_bytes()).hexdigest()
        languages = {".py": "python", ".rs": "rust", ".js": "javascript", ".ts": "typescript"}
        if path.suffix.lower() in languages:
            element = DocElement(element_id(source_id, "code/0"), "Code", path.read_text(encoding="utf-8"),
                                 attributes={"language": languages[path.suffix.lower()]})
            tree = build_tree(source_id, [element])
            routes = [PageRoute(1, "parse.native_code", "2.0", "code_text")]
        elif path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}:
            tree = self._paddle(path, source_id, page=1)
            routes = [PageRoute(1, self.paddle.parser_id, self.paddle.version, "image")]
        elif path.suffix.lower() != ".pdf":
            tree = self.docling.parse(path, source_id)
            pages = sorted({e.page for e in tree.elements if e.page is not None}) or [1]
            routes = [PageRoute(p, self.docling.parser_id, self.docling.version, "native_format") for p in pages]
        else:
            tree, routes = self._parse_pdf(path, source_id)
        return ParseResult(tree, ParserReceipt("parse.router", "2.0", routes, source_digest), source_metadata(path))

    def _parse_pdf(self, path: Path, source_id: str) -> tuple[DocElementTree, list[PageRoute]]:
        import pypdfium2 as pdfium
        import pypdfium2.raw as pdfium_raw

        native = self.docling.parse(path, source_id)
        table_pages = {p.get("page_no") for e in native.elements if e.kind == "Table"
                       for p in e.attributes.get("prov", [{"page_no": e.page}])}
        pdf = pdfium.PdfDocument(str(path))
        replacements, routes = {}, []
        try:
            count = len(pdf)
            with TemporaryDirectory(prefix="theorem-parse-") as temporary:
                for index in range(count):
                    page = pdf[index]
                    try:
                        textpage = page.get_textpage()
                        try:
                            text = textpage.get_text_range()
                        finally:
                            textpage.close()
                        features = PageFeatures(index + 1, text,
                            any(o.type == pdfium_raw.FPDF_PAGEOBJ_IMAGE for o in page.get_objects()),
                            index + 1 in table_pages)
                        if features.route == "parse.paddle":
                            bitmap = page.render(scale=2)
                            try:
                                image_path = Path(temporary) / f"{index + 1}.png"
                                bitmap.to_pil().save(image_path)
                            finally:
                                bitmap.close()
                            replacements[index + 1] = self._paddle(image_path, source_id, page=index + 1)
                            version = self.paddle.version
                        else:
                            version = self.docling.version
                        routes.append(PageRoute(index + 1, features.route, version, features.reason))
                    finally:
                        page.close()
            return merge_pages(source_id, native, replacements, count), routes
        finally:
            pdf.close()


def parse_text(text: str, source_id: str) -> ParseResult:
    """Captured plain text is already native text; no model is needed to lift it."""
    element = DocElement(element_id(source_id, "text/0"), "Paragraph", text)
    tree = build_tree(source_id, [element])
    receipt = ParserReceipt("parse.native_text", "2.0", [], sha256(text.encode()).hexdigest())
    return ParseResult(tree, receipt)
