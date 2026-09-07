"""Rebuild 100 authored fixtures; requires reportlab, python-docx, python-pptx.

These are authored test documents, not a sampled production dataset. Gold comes
from their source content. Parallel page illustrations are explicitly marked;
they are not asserted to be LibreOffice renderings of the office containers.
"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path


CATEGORIES = ("pdf", "scan", "warped_photo", "multi_column", "merged_table",
              "email", "docx", "pptx", "code", "html")
NAMES = ("Avery Morgan", "Jordan Lee", "Casey Rivera", "Morgan Ellis", "Taylor Chen",
         "Riley Brooks", "Alex Carter", "Drew Bennett", "Sam Patel", "Robin Wells")


def generate(output: Path, *, soffice: Path | None = None) -> None:
    if (output / "manifest.json").exists() and soffice is None and any(
            "renderer_receipt" in row for row in json.loads((output / "manifest.json").read_text())):
        raise ValueError("Refusing to replace a genuine Office render with authored parallel layout; supply --soffice")
    from reportlab.pdfgen.canvas import Canvas
    from docx import Document
    from pptx import Presentation
    from pptx.util import Inches, Pt
    import pypdfium2 as pdfium
    from PIL import Image

    output.mkdir(parents=True, exist_ok=True)
    manifest = []
    for category_index, category in enumerate(CATEGORIES):
        for index in range(10):
            identifier = f"{category}-{index + 1:02d}"
            reference = f"LEASE-{category_index + 1:02d}-{index + 1:03d}"
            person = NAMES[index]
            amount = 1250 + category_index * 125 + index * 35
            date = f"2026-{index % 9 + 1:02d}-15"
            title = f"Lease record {reference}"
            lines = [f"reference: {reference}", f"person: {person}", f"amount: {amount}",
                     f"deadline: {date}", f"{person} approved the lease."]
            pdf_path = output / f"{identifier}.pdf"
            canvas = Canvas(str(pdf_path), pagesize=(612, 792), invariant=1)
            canvas.setTitle(title)
            canvas.setFont("Helvetica-Bold", 16)
            canvas.drawString(48, 736, title)
            canvas.setFont("Helvetica", 12)
            for n, line in enumerate(lines):
                x, y = (48, 695 - 25 * n)
                if category == "multi_column" and n >= 3:
                    x, y = 320, 695 - 25 * (n - 3)
                canvas.drawString(x, y, line)
            tables, merged = [], []
            if category == "merged_table":
                canvas.rect(48, 420, 360, 90)
                canvas.line(48, 465, 408, 465)
                canvas.line(228, 420, 228, 465)
                canvas.drawString(60, 483, "Payment schedule")
                canvas.drawString(60, 438, str(amount))
                canvas.drawString(240, 438, "monthly")
                tables = [[["Payment schedule", ""], [str(amount), "monthly"]]]
                merged = [[{"row": 0, "column": 0, "row_span": 1, "col_span": 2}]]
            canvas.save()
            pdf = pdfium.PdfDocument(str(pdf_path))
            page = pdf[0]
            bitmap = page.render(scale=1.5)
            image = bitmap.to_pil().copy().convert("RGB")
            bitmap.close()
            page.close()
            pdf.close()
            png_path = output / f"{identifier}.png"
            if category == "warped_photo":
                image = image.transform(image.size, Image.Transform.PERSPECTIVE,
                    (1, .06, -8, .025, 1, -4, .000025, .00004), resample=Image.Resampling.BICUBIC,
                    fillcolor="white")
            image.save(png_path)
            path = pdf_path
            if category in {"scan", "warped_photo"}:
                path = png_path
            elif category == "email":
                path = output / f"{identifier}.eml"
                path.write_text(f"From: records@example.invalid\nTo: archive@example.invalid\nSubject: {title}\n"
                    f"Date: Tue, 01 Sep 2026 12:00:00 +0000\nMIME-Version: 1.0\n"
                    "Content-Type: text/plain; charset=utf-8\n\n" + "\n".join(lines) + "\n")
            elif category == "docx":
                path = output / f"{identifier}.docx"
                doc = Document()
                doc.add_heading(title, level=1)
                for line in lines:
                    doc.add_paragraph(line)
                doc.save(path)
            elif category == "pptx":
                path = output / f"{identifier}.pptx"
                doc = Presentation()
                slide = doc.slides.add_slide(doc.slide_layouts[6])
                for n, line in enumerate([title, *lines]):
                    box = slide.shapes.add_textbox(Inches(.5), Inches(.4 + n * .5), Inches(9), Inches(.4))
                    box.text_frame.text = line
                    box.text_frame.paragraphs[0].font.size = Pt(18 if n == 0 else 12)
                doc.save(path)
            elif category == "code":
                path = output / f"{identifier}.py"
                path.write_text(f'"""{title}\n' + "\n".join(lines) + '\n"""\n\n'
                    f'def payment_{index + 1}(months: int) -> int:\n    return {amount} * months\n')
            elif category == "html":
                path = output / f"{identifier}.html"
                path.write_text('<!doctype html><html><body><h1>'+title+'</h1>' +
                    ''.join('<p>'+line+'</p>' for line in lines)+'</body></html>')
            gold = {"texts": [title, *lines, *(["Payment schedule", str(amount), "monthly"] if tables else [])],
                "tables": tables, "merged_cells": merged, "headings": [[title, 1]],
                "fields": {"reference": {"class": "exact", "value": reference},
                           "person": {"class": "semantic", "value": person, "aliases": []},
                           "amount": {"class": "tolerance", "value": amount, "tolerance": .01},
                           "deadline": {"class": "exact", "value": date}},
                "spans": {"person": [person, person] if category != "code" else []},
                "relations": [[person, "approved", "lease"]],
                "claims": [{"text": f"{person} approved the lease.", "quote": f"{person} approved the lease.", "supported": True},
                           {"text": f"{person} rejected the lease.", "quote": f"{person} approved the lease.", "supported": False}]}
            gold_path = output / f"{identifier}.gold.json"
            gold_path.write_text(json.dumps(gold, indent=2) + "\n")
            manifest.append({"id": identifier, "category": category,
                "source": path.name, "source_sha256": sha256(path.read_bytes()).hexdigest(),
                "comparison_pdf": pdf_path.name, "comparison_pdf_sha256": sha256(pdf_path.read_bytes()).hexdigest(),
                "page_image": png_path.name, "page_image_sha256": sha256(png_path.read_bytes()).hexdigest(),
                "page_image_provenance": "authored_parallel_layout" if category in {"docx", "pptx", "email", "code", "html"} else "pdfium_rendered",
                "gold": gold_path.name, "gold_sha256": sha256(gold_path.read_bytes()).hexdigest(),
                "corpus_class": "authored_fixture"})
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    if soffice is not None:
        from theorem_ml.extract.bench.render_docx_fixture import render
        render(output, soffice)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "corpus")
    parser.add_argument("--soffice", type=Path)
    args = parser.parse_args()
    generate(args.output, soffice=args.soffice)
