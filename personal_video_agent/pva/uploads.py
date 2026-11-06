"""Private uploaded sources; never passed to internet video lookup."""
from __future__ import annotations

import hashlib
from pathlib import Path

AUDIO = {"mp3", "wav", "m4a", "aac", "flac", "ogg", "opus", "wma"}
DOCUMENTS = {"txt", "md", "pdf", "docx"}
EXTENSIONS = sorted(AUDIO | DOCUMENTS)


def save(db, root: Path, name: str, content: bytes, language: str, route):
    from . import store, tasks
    suffix = Path(name).suffix.lower()
    if suffix.lstrip(".") not in EXTENSIONS:
        raise ValueError("Unsupported file type.")
    if not content:
        raise ValueError("The file is empty.")
    route.register()
    digest = hashlib.sha256(content).hexdigest()
    folder = root / "uploads"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (digest + suffix)
    path.write_bytes(content)
    video_id = store.add_video(db, "pva-upload://" + digest + suffix,
                               title=Path(name.replace("\\", "/")).name,
                               target_language=language)
    with db:
        db.execute("INSERT OR REPLACE INTO uploaded_sources(video_id,path,kind) VALUES(?,?,?)",
                   (video_id, str(path.resolve()), "audio" if suffix[1:] in AUDIO else "document"))
    active = db.execute("SELECT id FROM processing_tasks WHERE video_id=? AND status IN ('queued','running')",
                        (video_id,)).fetchone()
    if active:
        return active["id"]
    return tasks.enqueue_processing_video(db, video_id, summary_route=route)


def document_segments(path: Path) -> list[dict]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise ValueError("This PDF is password protected. Upload an unlocked copy.")
        blocks = [(page.extract_text() or "").strip() for page in reader.pages]
        if any(not block for block in blocks):
            raise ValueError("This PDF contains pages without extractable text. Upload a searchable PDF; scanned pages need OCR.")
    elif suffix == ".docx":
        from docx import Document
        from docx.oxml.ns import qn
        from docx.table import Table
        from docx.text.paragraph import Paragraph
        document = Document(path)
        blocks = []
        for child in document.element.body.iterchildren():
            if child.tag == qn("w:p"):
                blocks.append(Paragraph(child, document).text)
            elif child.tag == qn("w:tbl"):
                blocks.extend(" | ".join(cell.text for cell in row.cells)
                              for row in Table(child, document).rows)
    else:
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig")
        except UnicodeDecodeError:
            raise ValueError("Save the text document as UTF-8 or UTF-16 and upload it again.") from None
        blocks = text.split("\n\n")
    # One evidence unit per source block, bounded for long pages/paragraphs.
    chunks = [block[i:i+2000].strip() for block in blocks for i in range(0, len(block), 2000)]
    chunks = [chunk for chunk in chunks if chunk]
    if not chunks:
        raise ValueError("No readable text found in this document.")
    return [{"start": float(i), "end": float(i+1), "text": text}
            for i, text in enumerate(chunks)]
