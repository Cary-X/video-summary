"""Reader-facing summary and transcript formatting."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from html import escape

from markdown_it import MarkdownIt


_EVIDENCE_SUFFIX = re.compile(r"\s*\((?:(?:s|f):\d+(?:,\s*)?)+\)\s*$")
_VIDEO_DATE = re.compile(r"(\d{4})[-/.]?(\d{2})[-/.]?(\d{2})")
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
        for group in matched:
            source_parts.append(f'<p>{escape(group["text"])}</p>')
        for frame in matched_frames:
            source_parts.append('<p><strong>Visual evidence</strong>'
                                f'<br>{escape(frame["extracted_text"])}</p>')
        bullet = line.startswith("- ")
        body = markdown.render(line[2:] if bullet else line)
        marker = ""
        if source_parts:
            marker = ('<details class="pva-source"><summary aria-label="Show supporting sources" '
                      'title="Show supporting sources">Δ</summary>'
                      f'<div class="pva-source-panel"><strong>Supporting sources · {span}</strong>'
                      '<span class="pva-source-hint">Drag the bottom-right corner to resize · Click Δ to close</span>' +
                      "".join(source_parts) + '</div></details>')
        parts.append('<div class="pva-point">' + ('<span aria-hidden="true">• </span>' if bullet else '') +
                     '<div class="pva-copy">' + body + '</div>' + marker + '</div>')
    return f"""<style>
.pva-summary {{ font-size:{int(font_size)}px; line-height:1.6; }}
.pva-point {{ position:relative; margin:0 0 .85em; }}
.pva-copy, .pva-copy > p {{ display:inline; margin:0; font-size:inherit; }}
.pva-source {{ display:inline; margin-left:.35em; }}
.pva-source > summary {{ display:inline; list-style:none; cursor:pointer; color:#168bba;
  font-size:.78em; font-weight:700; padding:.05em .3em; border-radius:4px; }}
.pva-source > summary::-webkit-details-marker {{ display:none; }}
.pva-source > summary:hover, .pva-source[open] > summary {{ background:rgba(22,139,186,.15); }}
.pva-source:focus-within > .pva-source-panel {{ z-index:110; }}
.pva-source-panel {{ position:absolute; top:100%; left:0; z-index:100; box-sizing:border-box;
  width:min(680px,100%); height:320px; resize:both; overflow:auto; padding:16px 18px 24px;
  min-width:min(280px,100%); max-width:min(960px,100%);
  min-height:140px; max-height:min(640px,75vh);
  background:#fff !important; color:#222 !important; opacity:1 !important;
  border:1px solid #647783; border-radius:8px; box-shadow:0 8px 28px #0003;
  font-size:.9em; line-height:1.5; }}
.pva-source-panel p {{ margin:.8em 0 0; font-size:inherit !important; }}
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
