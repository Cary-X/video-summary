"""Reader-facing summary and transcript formatting."""
from __future__ import annotations

import re


_EVIDENCE_SUFFIX = re.compile(r"\s*\((?:(?:s|f):\d+(?:,\s*)?)+\)\s*$")


def clean_summary(summary: str) -> str:
    """Hide legacy internal citations without altering their stored audit trail."""
    return "\n".join(_EVIDENCE_SUFFIX.sub("", line) for line in summary.splitlines())


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
