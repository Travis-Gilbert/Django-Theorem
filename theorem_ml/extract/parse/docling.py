"""Production Docling conversion with OCR disabled on the native PDF path."""

from importlib.metadata import version
from pathlib import Path

from ..elements import DocElementTree, from_docling


class DoclingParser:
    parser_id = "parse.docling"

    def __init__(self) -> None:
        self._converter = None

    @property
    def version(self) -> str:
        return version("docling")

    def parse(self, path: Path, source_id: str) -> DocElementTree:
        if self._converter is None:
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import PdfPipelineOptions
            from docling.document_converter import DocumentConverter, PdfFormatOption

            options = PdfPipelineOptions(do_ocr=False, do_table_structure=True)
            self._converter = DocumentConverter(format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=options),
            })
        result = self._converter.convert(Path(path), raises_on_error=True)
        status = getattr(result.status, "value", str(result.status))
        if status != "success":
            raise RuntimeError(f"Docling conversion did not succeed: {status}")
        return from_docling(result.document, source_id)
