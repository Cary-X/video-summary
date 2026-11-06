"""Reader-facing summary and transcript formatting."""
from __future__ import annotations

import re
from base64 import b64encode
from datetime import datetime, timezone
from html import escape

from markdown_it import MarkdownIt

from . import visual_evidence


_EVIDENCE_SUFFIX = re.compile(r"\s*\((?:(?:s|f):\d+(?:,\s*)?)+\)\s*$")
_VIDEO_DATE = re.compile(r"(\d{4})[-/.]?(\d{2})[-/.]?(\d{2})")
_TV_ICON = b64encode(b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 28 25">
  <g stroke="#51384d" stroke-linecap="round">
    <path d="M10 5 7 1m6 4 4-4M7 21l-2 3m15-3 2 3" fill="none" stroke-width="1.7"/>
    <rect x="2" y="5" width="24" height="16" rx="4" fill="#cdb2ca" stroke-width="1.6"/>
    <rect x="5" y="8" width="14" height="10" rx="3" fill="#faf0f8" stroke-width="1.2"/>
    <path d="M9 12.5h6m-6 2.5h4" stroke-width="1.1"/>
  </g>
  <circle cx="22.5" cy="11" r="1" fill="#51384d"/>
  <circle cx="22.5" cy="15.5" r="1" fill="#51384d"/>
</svg>""").decode("ascii")
CATEGORY_LABELS = {
    "technology": "Tech", "medicine": "Medical", "finance": "Finance",
    "politics": "Politics", "religion": "Religion", "music": "Music",
    "lifestyle": "Lifestyle", "sports": "Sports", "health": "Health",
    "policy": "Policy", "science": "Science", "education": "Education",
    "culture": "Culture", "business": "Business", "other": "Other",
}


def category_label(category: str | None) -> str:
    return CATEGORY_LABELS.get(category or "", (category or "Unclassified").title())


def order_library(videos: list[dict], *, date_field: str,
                  newest_first: bool) -> list[dict]:
    """Sort by publication or local add time, always leaving unknown dates last."""
    if date_field not in {"published_at", "created_at"}:
        raise ValueError("Unsupported library date field")

    def timestamp(video: dict) -> float | None:
        raw = str(video.get(date_field) or "").strip()
        try:
            if date_field == "created_at" or "T" in raw or " " in raw:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp()
            match = _VIDEO_DATE.search(raw)
            if match:
                return datetime(*map(int, match.groups()), tzinfo=timezone.utc).timestamp()
        except ValueError:
            pass
        return None

    def key(video: dict):
        value = timestamp(video)
        return (value is None, (-value if newest_first else value) if value is not None else 0,
                str(video.get("title") or "").casefold(), str(video.get("id") or ""))

    return sorted(videos, key=key)


def clean_summary(summary: str) -> str:
    """Hide internal citations and stray code-style backticks in reader text."""
    return "\n".join(_EVIDENCE_SUFFIX.sub("", line).replace("`", "")
                     for line in summary.splitlines())


def summary_html(video: dict, *, font_size: int = 16) -> str:
    """Render ordinary currency and point-level sources outside the reading flow."""
    markdown = MarkdownIt("commonmark", {"html": False, "linkify": False})
    frames = {f"f:{row['id']}": row for row in video.get("frames", [])}
    sentences = transcript_sentences(video.get("segments", []))

    def normalized(text: str) -> str:
        return re.sub(r"\s+", " ", clean_summary(text).strip().lstrip("- ")
                      .replace("*", "").replace("_", "")).strip()

    claims: dict[str, list[str]] = {}
    for claim in video.get("claims", []):
        claims.setdefault(normalized(claim["text"]), []).extend(claim["evidence"])
    parts = []
    for raw in (video.get("summary") or "").splitlines():
        if not raw.strip():
            continue
        line = clean_summary(raw).strip()
        refs = list(dict.fromkeys(claims.get(normalized(line), [])))
        # Older summaries may carry citations directly instead of matching claims.
        suffix = _EVIDENCE_SUFFIX.search(raw)
        if suffix:
            refs += [ref for ref in re.findall(r"[sf]:\d+", suffix.group()) if ref not in refs]
        source_parts = []
        matched = source_passages(sentences, refs)
        matched_frames = [frames[ref] for ref in refs if ref in frames]
        starts = [row["start"] for row in matched] + [row["at_seconds"] for row in matched_frames]
        ends = [row["end"] for row in matched] + [row["at_seconds"] for row in matched_frames]
        span = f"{timestamp(min(starts))}–{timestamp(max(ends))}" if starts else ""
        if video.get("transcript_source") == "document":
            matched = [row for row in video.get("segments", []) if f"s:{row['id']}" in refs]
            span = "Document text"
        for group in matched:
            source_parts.append(f'<p>{escape(group["text"])}</p>')
        for frame in matched_frames:
            source_parts.append('<section><strong>Visual evidence</strong><br>'
                                + visual_evidence.render(frame["extracted_text"], html=True) + '</section>')
        bullet = line.startswith("- ")
        body = markdown.render(line[2:] if bullet else line)
        marker = ""
        if source_parts:
            marker = ('<details class="pva-source"><summary aria-label="Show supporting sources" '
                      'title="Show supporting sources"></summary>'
                      f'<div class="pva-source-panel"><strong>Supporting sources · {span}</strong>'
                      '<span class="pva-source-hint">Drag the bottom-right corner to resize · Select the TV again to close</span>' +
                      "".join(source_parts) + '</div></details>')
        parts.append('<div class="pva-point">' + ('<span aria-hidden="true">• </span>' if bullet else '') +
                     '<div class="pva-copy">' + body + '</div>' + marker + '</div>')
    return f"""<style>
.pva-summary {{ font-size:{int(font_size)}px; line-height:1.6; }}
.pva-point {{ position:relative; margin:0 0 .85em; }}
.pva-copy, .pva-copy > p {{ display:inline; margin:0; font-size:inherit; }}
.pva-source {{ display:inline; margin-left:.35em; }}
.pva-source > summary {{ display:inline-flex; vertical-align:middle; list-style:none;
  cursor:pointer; color:#694c3d; padding:.15em .27em; border-radius:.45em; }}
.pva-source > summary::before {{ content:""; width:1.35em; height:1.23em; display:block;
  background: url("data:image/svg+xml;base64,{_TV_ICON}") center / contain no-repeat; }}
.pva-source > summary::-webkit-details-marker {{ display:none; }}
.pva-source > summary:hover, .pva-source[open] > summary {{ background:#eddfeb; }}
.pva-source:focus-within > .pva-source-panel {{ z-index:110; }}
.pva-source-panel {{ position:absolute; top:100%; left:0; z-index:100; box-sizing:border-box;
  width:min(680px,100%); height:320px; resize:both; overflow:auto; padding:16px 18px 24px;
  min-width:min(280px,100%); max-width:min(960px,100%);
  min-height:140px; max-height:min(640px,75vh);
  background:#fff !important; color:#222 !important; opacity:1 !important;
  border:1px solid #647783; border-radius:8px; box-shadow:0 8px 28px #0003;
  font-size:.9em; line-height:1.5; }}
.pva-source-panel p {{ margin:.8em 0 0; font-size:inherit !important; }}
.pva-source-panel table {{ border-collapse:collapse; margin:.55em 0; }}
.pva-source-panel :is(th,td) {{ border:1px solid #b3a2b0; padding:.3em .5em; text-align:left; }}
.pva-source-hint {{ display:block; font-size:.8em; opacity:.65; margin-top:3px; }}
</style><div class="pva-summary">{"".join(parts)}</div>"""


def timestamp(seconds: float) -> str:
    value = int(seconds)
    return f"{value // 3600:02d}:{value // 60 % 60:02d}:{value % 60:02d}"


def _word(word: str) -> str:
    return "".join(c for c in word.casefold() if c.isalnum())


def _append_without_overlap(current: str, incoming: str) -> str:
    existing = current.split()
    new = incoming.split()
    limit = min(len(existing), len(new), 30)
    overlap = 0
    for count in range(limit, 0, -1):
        if [_word(x) for x in existing[-count:]] == [_word(x) for x in new[:count]]:
            overlap = count
            break
    return " ".join(existing + new[overlap:]).strip()


def transcript_sentences(rows: list[dict]) -> list[dict]:
    """Rebuild source sentences before selecting evidence from sliding captions.

    Each word retains its originating segment IDs. A cited caption fragment then
    selects its whole sentence, including neighboring captions, without rewriting
    speech or stitching together unrelated parts of the video.
    """
    sentences = []
    words: list[dict] = []
    previous_end = None

    def flush() -> None:
        sentence = []
        for word in words:
            sentence.append(word)
            if re.search(r'''[.!?。！？]["'”’）)\]]*$''', word["text"]):
                emit(sentence)
                sentence = []
        if sentence:
            emit(sentence)
        words.clear()

    def emit(items: list[dict]) -> None:
        text = " ".join(item["text"] for item in items)
        text = re.sub(r"(?<=[\u3400-\u9fff。，！？、；：]) (?=[\u3400-\u9fff。，！？、；：])", "", text)
        sentences.append({"text": text, "start": min(x["start"] for x in items),
                          "end": max(x["end"] for x in items),
                          "evidence": set().union(*(x["evidence"] for x in items))})

    for row in sorted(rows, key=lambda x: (x["start"], x["end"])):
        if previous_end is not None and row["start"] > previous_end + 1.5:
            flush()
        # Split CJK sentence endings as well as whitespace-delimited captions.
        incoming = re.findall(r"[\u3400-\u9fff]|[^\s\u3400-\u9fff。！？]+|[。！？]",
                              str(row.get("text") or ""))
        overlap = 0
        for count in range(min(len(words), len(incoming), 30), 0, -1):
            if [(_word(x["text"]) or x["text"]) for x in words[-count:]] == [
                    (_word(x) or x) for x in incoming[:count]]:
                overlap = count
                break
        ref = f"s:{row['id']}"
        if overlap:
            for item in words[-overlap:]:
                item["evidence"].add(ref)
                item["end"] = max(item["end"], row["end"])
        words.extend({"text": text, "start": row["start"], "end": row["end"],
                      "evidence": {ref}} for text in incoming[overlap:])
        previous_end = max(previous_end or row["end"], row["end"])
    flush()
    return sentences


def source_passages(sentences: list[dict], refs: list[str]) -> list[dict]:
    """Keep neighboring cited sentences together; preserve gaps between excerpts."""
    passages = []
    last_index = -2
    for index, sentence in enumerate(sentences):
        if not sentence["evidence"].intersection(refs):
            continue
        if (passages and index == last_index + 1 and
                sentence["start"] <= passages[-1]["end"] + 1.5):
            passages[-1]["text"] += " " + sentence["text"]
            passages[-1]["end"] = max(passages[-1]["end"], sentence["end"])
        else:
            passages.append({key: sentence[key] for key in ("text", "start", "end")})
        last_index = index
    return passages


def merge_transcript(rows: list[dict], *, max_span: float = 25.0) -> list[dict]:
    """Join neighboring sliding captions and remove repeated leading words."""
    merged: list[dict] = []
    for row in sorted(rows, key=lambda item: (item["start"], item["end"])):
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        start, end = float(row["start"]), float(row["end"])
        if (merged and start <= merged[-1]["end"] + 1.5 and
                end - merged[-1]["start"] <= max_span):
            merged[-1]["end"] = max(merged[-1]["end"], end)
            merged[-1]["text"] = _append_without_overlap(merged[-1]["text"], text)
        else:
            merged.append({"start": start, "end": end, "text": text})
    return merged
