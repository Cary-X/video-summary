"""Portable summary documents and email drafts generated from saved evidence."""
from email.message import EmailMessage
from html import escape
from io import BytesIO
import re
from urllib.parse import urlsplit

from . import library, presentation, visual_evidence

FORMATS = {
    "PDF": ("pdf", "application/pdf"),
    "Word": ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    "HTML": ("html", "text/html"),
    "Markdown": ("md", "text/markdown"),
    "Plain text": ("txt", "text/plain"),
}


def build_sections(video, options, collections=()):
    source_kind = video["source_kind"]
    document = source_kind in {"document", "webpage"}
    sections = []
    if "Source details" in options:
        parts = [library.source_label(video), presentation.category_label(video.get("category"))]
        parts.extend(str(video[key]) for key in ("author", "published_at") if video.get(key))
        if urlsplit(video["url"]).scheme in {"http", "https"}:
            parts.append(video["url"])
        if collections:
            parts.append("Collections: " + ", ".join(collections))
        sections.append(("Source details", parts))
    summary = presentation.clean_summary(video.get("summary") or "")
    lines = [line.strip().replace("**", "").replace("__", "") for line in summary.splitlines() if line.strip()]
    sections.append(("Summary", lines))
    rows = video.get("segments", [])
    if "Supporting evidence" in options:
        evidence = {ref for claim in video.get("claims", []) for ref in claim["evidence"]}
        if document:
            excerpts = [row["text"] for row in rows if f"s:{row['id']}" in evidence]
        else:
            passages = presentation.source_passages(presentation.transcript_sentences(rows), evidence)
            excerpts = [_timed(row) for row in passages]
        excerpts.extend(visual_evidence.render(frame["extracted_text"]) for frame in video.get("frames", [])
                        if f"f:{frame['id']}" in evidence and frame["extracted_text"])
        if excerpts:
            sections.append(("Supporting evidence", excerpts))
    if "Full source text" in options and rows:
        sections.append(("Full document text" if document else "Full transcript",
                         [row["text"] for row in rows] if document else
                         [_timed(row) for row in presentation.merge_transcript(rows)]))
    if "Translated transcript" in options:
        translated = [{**row, "text": row["translated_text"]} for row in rows if row.get("translated_text")]
        if translated:
            sections.append(("Translated transcript", [_timed(row) for row in presentation.merge_transcript(translated)]))
    if "Visual text" in options and video.get("frames"):
        sections.append(("Visual text", [
            presentation.timestamp(frame["at_seconds"]) + "  " + visual_evidence.render(frame["extracted_text"])
            for frame in video["frames"] if frame["extracted_text"]
        ]))
    return sections


def _timed(row):
    start, end = presentation.timestamp(row["start"]), presentation.timestamp(row["end"])
    return f"{start}–{end}  {row['text']}"


def filename(video, extension):
    day = library.source_date(video.get("published_at")) or library.source_date(video.get("created_at"))
    title = re.sub(r'[^\w .-]', " ", video["title"], flags=re.UNICODE)
    title = " ".join(title.split()).strip(" .-")[:80].rstrip(" .")
    return f"{day or 'undated'} - {title or 'Summary'} - {video['id'][:8]}.{extension}"


def plain_text(title, sections):
    return "\n\n".join([title] + [heading + "\n" + "\n\n".join(lines) for heading, lines in sections])


def render(title, sections, format_name):
    # Some copied document text contains control characters that XML cannot store.
    def clean(value):
        return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value)

    title = clean(title)
    sections = [(heading, [clean(line) for line in lines]) for heading, lines in sections]
    if format_name == "Plain text":
        return plain_text(title, sections).encode("utf-8-sig")
    if format_name == "Markdown":
        return "\n\n".join(["# " + title] + ["## " + heading + "\n\n" + "\n\n".join(lines)
                             for heading, lines in sections]).encode("utf-8")
    if format_name == "HTML":
        content = "".join("<section><h2>" + escape(heading) + "</h2>" +
                          "".join("<p>" + escape(line) + "</p>" for line in lines) + "</section>"
                          for heading, lines in sections)
        return (f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" '
                f'content="width=device-width, initial-scale=1"><title>{escape(title)}</title>'
                '<style>body{margin:0;background:#faf1e5;color:#302319;font:17px/1.65 system-ui,sans-serif}'
                'main{max-width:54rem;margin:auto;padding:2.75rem 1.5rem}header{border-bottom:2px solid #ae8565}'
                'h1,h2{font-family:Georgia,serif;line-height:1.2;overflow-wrap:anywhere}'
                'h1{font-size:2.45rem;margin:.3rem 0 1.2rem}h2{color:#714126;margin-top:2rem}'
                'p{white-space:pre-wrap;overflow-wrap:anywhere}small{color:#714126;font-weight:700}'
                '@media print{body{background:white}main{padding:0}h2{break-after:avoid}}'
                '</style></head><body><main><header><small>Raccoon Agent</small>'
                f'<h1>{escape(title)}</h1></header>{content}</main></body></html>').encode("utf-8")
    if format_name == "Word":
        return _word(title, sections)
    if format_name == "PDF":
        return _pdf(title, sections)
    raise ValueError("Unsupported summary format.")


def _word(title, sections):
    from docx import Document
    from docx.shared import Pt, RGBColor

    doc = Document()
    doc.core_properties.title = title
    doc.core_properties.author = "Raccoon Agent"
    for name in ("Title", "Heading 1"):
        doc.styles[name].font.color.rgb = RGBColor.from_string("714126")
    doc.styles["Normal"].font.size = Pt(11)
    doc.add_heading(title, 0)
    for heading, lines in sections:
        doc.add_heading(heading, 1)
        for line in lines:
            if line.startswith(("- ", "• ")):
                doc.add_paragraph(line[2:], style="List Bullet")
            else:
                doc.add_paragraph(line)
    doc.sections[0].footer.paragraphs[0].text = "Raccoon Agent"
    output = BytesIO()
    doc.save(output)
    return output.getvalue()


def _pdf(title, sections):
    from reportlab.lib.colors import HexColor
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate

    font = "STSong-Light"
    pdfmetrics.registerFont(UnicodeCIDFont(font))
    base = ParagraphStyle("Body", fontName=font, fontSize=11, leading=17, wordWrap="CJK",
                          spaceAfter=8, textColor=HexColor("#302319"))
    heading_style = ParagraphStyle("Heading", parent=base, fontSize=15, leading=20,
                                   spaceBefore=12, textColor=HexColor("#714126"), keepWithNext=True)
    title_style = ParagraphStyle("Title", parent=heading_style, fontSize=24, leading=31, spaceAfter=18)

    def paragraph(text, style):
        return Paragraph(escape(text).replace("\n", "<br/>"), style)

    def footer(canvas, doc):
        canvas.setFont("Helvetica", 9)
        canvas.setFillColor(HexColor("#714126"))
        canvas.drawString(48, 30, "Raccoon Agent")
        canvas.drawRightString(A4[0] - 48, 30, str(doc.page))

    output = BytesIO()
    doc = SimpleDocTemplate(output, pagesize=A4, title=title, author="Raccoon Agent",
                            leftMargin=48, rightMargin=48, topMargin=48, bottomMargin=48)
    story = [paragraph(title, title_style)]
    for heading, lines in sections:
        story.append(paragraph(heading, heading_style))
        story.extend(paragraph(line, base) for line in lines)
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()


def email_draft(title, body, data, name, mime):
    message = EmailMessage()
    message["Subject"] = " ".join(title.split())
    message["X-Unsent"] = "1"
    message.set_content(body)
    main, subtype = mime.split("/", 1)
    message.add_attachment(data, maintype=main, subtype=subtype, filename=name)
    return message.as_bytes()
