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


def document_segments(path: Path, *, on_progress=None) -> list[dict]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from .document_reader import pdf_blocks
        blocks = pdf_blocks(path, on_progress=on_progress)
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
                from .document_reader import table_text
                blocks.append(table_text([[cell.text for cell in row.cells]
                                           for row in Table(child, document).rows]))
        from .document_reader import image_text
        for index, relation in enumerate(document.part.rels.values()):
            if relation.reltype.endswith("/image") and not relation.is_external:
                image_path = path.parent / (path.stem + f"-image-{index}.png")
                from PIL import Image
                from io import BytesIO
                with Image.open(BytesIO(relation.target_part.blob)) as image:
                    image.convert("RGB").save(image_path)
                if on_progress:
                    on_progress(f"Reading document image {index + 1}")
                blocks.append(f"Document image {index + 1}\n" + image_text(image_path, on_progress))
    else:
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig")
        except UnicodeDecodeError:
            raise ValueError("Save the text document as UTF-8 or UTF-16 and upload it again.") from None
        blocks = text.split("\n\n")
    return text_segments(blocks)


def text_segments(blocks: list[str]) -> list[dict]:
    # Source labels stay with every evidence chunk.
    chunks = [block.split("\n", 1)[0] + "\n" + block[i:i+2000].strip()
              if i else block[i:i+2000].strip()
              for block in blocks for i in range(0, len(block), 2000)]
    chunks = [chunk for chunk in chunks if chunk]
    if not chunks:
        raise ValueError("No readable text found in this document.")
    return [{"start": float(i), "end": float(i+1), "text": text}
            for i, text in enumerate(chunks)]
