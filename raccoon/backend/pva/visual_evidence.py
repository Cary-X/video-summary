"""Validate structured visual evidence and render it without trusting model HTML."""
from __future__ import annotations

import base64
import re
from html import escape
from html.parser import HTMLParser
from pathlib import Path


KINDS = ("illustration", "chart", "table", "text", "unreadable")
PROMPT = (
    'Read this screenshot as evidence. Return JSON with "kind", "text", and "rows". '
    'Use kind "table" for a visible grid or aligned columns of data, and transcribe each cell '
    'verbatim in rows, including the header. Keep the original language and exact numbers. '
    'Use kind "chart" only for a plot with explicit axis labels, a scale, or printed data values. '
    'Record exact numbers only when printed as data labels or shown in an accompanying table. '
    'Do not estimate numbers from bar heights or tick marks. Keep visible units and series names. '
    'A row of decorative shapes, an architecture diagram, and a flowchart without plotted data '
    'are illustrations: use kind "illustration" and rows []. Never invent numbers or cells. '
    'Put readable labels or a short faithful description in text. For unreadable cells use "?" '
    'and use kind "unreadable" if nothing can be read. Do not translate or infer missing values.'
)


def messages(image: Path) -> list[dict]:
    mime = "image/png" if image.suffix.lower() == ".png" else "image/jpeg"
    encoded = base64.b64encode(image.read_bytes()).decode("ascii")
    return [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
        {"type": "text", "text": PROMPT},
    ]}]


def format_result(result: dict) -> str:
    kind, description, rows = result.get("kind"), result.get("text"), result.get("rows")
    if kind not in KINDS or not isinstance(description, str) or not isinstance(rows, list):
        raise ValueError("Vision model returned an invalid evidence record.")
    if kind not in {"chart", "table"} and rows:
        raise ValueError("Vision model returned rows for an image without tabular data.")
    if kind == "table" and not rows:
        raise ValueError("Vision model identified a table but could not extract its rows.")
    if rows and (not isinstance(rows[0], list) or not rows[0] or not all(
        isinstance(row, list) and len(row) == len(rows[0])
        and all(isinstance(cell, str) for cell in row) for row in rows
    )):
        raise ValueError("Vision model returned inconsistent table columns.")
    body = f"[{kind}] {description.strip()}"
    if rows:
        body += "\n\n[table] " + _table_html(rows)
    return body


class _Cells(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self.row: list[str] | None = None
        self.cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag in {"td", "th"} and self.row is not None:
            self.cell = []
        elif tag == "br" and self.cell is not None:
            self.cell.append(" ")

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.cell is not None and self.row is not None:
            self.row.append("".join(self.cell).strip())
            self.cell = None
        elif tag == "tr" and self.row:
            self.rows.append(self.row)
            self.row = None


_TABLE = re.compile(r"\[table\]\s*(<table\b[^>]*>.*?</table\s*>)", re.IGNORECASE | re.DOTALL)
_LEGACY_CHART = re.compile(r"(?ms)^\[chart\][ \t]+([^\n].*?)(?=^\[[a-z_ ]+\]|\Z)")


def _table_html(rows: list[list[str]]) -> str:
    output = []
    for index, row in enumerate(rows):
        cell = "th" if index == 0 else "td"
        output.append("<tr>" + "".join(f"<{cell}>{escape(value)}</{cell}>" for value in row) + "</tr>")
    return "<table>" + "".join(output) + "</table>"


def _remove_suspect_chart(text: str) -> str:
    def replace(match: re.Match) -> str:
        lines = match[1].splitlines()
        cells = [cell.strip() for line in lines[1:] for cell in line.split("|")[1:]]
        values = [cell for cell in cells if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", cell)]
        if len(values) >= 30 and len(set(values)) == 1 and len(values) >= 0.9 * len(cells):
            return "[chart] Unverified chart extraction omitted. See the original screenshot.\n\n"
        return match[0]

    return _LEGACY_CHART.sub(replace, text)


def render(text: str, *, html: bool = False) -> str:
    """Render stored tables, including legacy PaddleOCR HTML, as cells or text."""
    text = _remove_suspect_chart(text)
    parts = []
    cursor = 0
    for match in _TABLE.finditer(text):
        plain = text[cursor:match.start()]
        parts.append(escape(plain).replace("\n", "<br>") if html else plain)
        parser = _Cells()
        parser.feed(match[1])
        if parser.rows:
            if html:
                parts.append('<div class="pva-visual-table">' + _table_html(parser.rows) + '</div>')
            else:
                parts.append("\n".join(" | ".join(row) for row in parser.rows))
        cursor = match.end()
    remaining = text[cursor:]
    parts.append(escape(remaining).replace("\n", "<br>") if html else remaining)
    return "".join(parts)
