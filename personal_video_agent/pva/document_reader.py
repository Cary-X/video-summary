"""Extract page text, explicit table cells and OCR evidence without inventing values."""
from pathlib import Path


def table_text(rows):
    rows = [[str(cell or "").strip().replace("\n", " ") for cell in row] for row in rows]
    if not rows:
        return ""
    header = rows[0]
    lines = ["Table", " | ".join(header)]
    for row in rows[1:]:
        lines.append(" | ".join(f"{header[i] if i < len(header) and header[i] else f'Column {i+1}'}: {cell}"
                                for i, cell in enumerate(row)))
    return "\n".join(lines)


def image_text(path, on_progress=None):
    from . import intelligence, visual_evidence
    text = intelligence.read_frame(path, on_progress=on_progress)
    # Keep visible text plus structured table cells in readable form.
    rendered = visual_evidence.render(text, html=False)
    if not rendered.strip() or "[unreadable]" in rendered:
        raise ValueError("OCR returned no readable evidence.")
    return rendered


def pdf_blocks(path: Path, *, on_progress=None):
    import pdfplumber
    import pypdfium2 as pdfium
    from pypdf import PdfReader
    if PdfReader(path).is_encrypted:
        raise ValueError("This PDF is password protected. Upload an unlocked copy.")
    blocks = []
    with pdfplumber.open(path) as pdf:
        for index, page in enumerate(pdf.pages):
            label = f"Page {index + 1}"
            if on_progress:
                on_progress(f"Extracting {label} of {len(pdf.pages)}")
            text = (page.extract_text(layout=True) or "").strip()
            tables = page.extract_tables()
            if not text and not page.images and not page.curves and not page.rects and not page.lines:
                if on_progress:
                    on_progress(f"{label} is blank; skipped")
                continue
            if text:
                blocks.append(label + "\n" + text)
            blocks.extend(label + "\n" + table_text(rows) for rows in tables if rows)
            # Scans, embedded figures and broken font mappings require visual reading.
            if not text or "\ufffd" in text or "(cid:" in text or page.images:
                image_path = path.parent / (path.stem + f"-page-{index+1}.png")
                with pdfium.PdfDocument(path) as rendered_pdf:
                    rendered_page = rendered_pdf[index]
                    scale = min(2.5, 2400 / max(rendered_page.get_size()))
                    bitmap = rendered_page.render(scale=scale)
                    bitmap.to_pil().save(image_path)
                    bitmap.close()
                    rendered_page.close()
                blocks.append(label + " (visual evidence)\n" + image_text(image_path, on_progress))
    if not blocks:
        raise ValueError("No readable pages found in this PDF; there is no content for OCR.")
    return blocks
