"""Reader-facing summary and transcript formatting."""
from __future__ import annotations

import re
from datetime import datetime, timezone


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
