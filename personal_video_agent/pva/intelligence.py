"""Translation, grounded summarization, visual reading, and categorization."""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse
from collections.abc import Callable

from . import store
from .media import same_language


CATEGORIES = ("finance", "sports", "technology", "policy", "science", "health",
              "education", "culture", "business", "other")
LANGUAGE_NAMES = {"en": "English", "zh-Hans": "简体中文", "zh-Hant": "繁體中文",
                  "es": "Español", "yue": "粵語（香港繁體中文口語）"}
_usage_context: ContextVar[tuple[Path, str | None, str | None] | None] = ContextVar(
    "pva_usage_context", default=None)


@contextmanager
def usage_context(db_path: Path, *, video_id: str | None = None,
                  finding_task_id: str | None = None):
    token = _usage_context.set((db_path, video_id, finding_task_id))
    try:
        yield
    finally:
        _usage_context.reset(token)


@lru_cache(maxsize=1)
def _ocr_engine():
    from rapidocr import RapidOCR
    return RapidOCR()


def local_ocr(image: Path) -> str:
    try:
        result = _ocr_engine()(str(image))
        return "\n".join(result.txts or ()).strip()
    except Exception:
        return ""


def identify_video_photo(image: Path) -> dict:
    """Use the actual image to separate distinct, readable video cards."""
    ocr = local_ocr(image)
    mime = "image/png" if image.suffix.casefold() == ".png" else "image/jpeg"
    encoded = base64.b64encode(image.read_bytes()).decode("ascii")
    result = _json(chat([
        {"role": "system", "content": (
            "Inspect the image itself, using OCR only as a reading aid. Identify EACH distinct, "
            "complete video card whose title or creator is readable. A single photo may show "
            "several videos. Ignore page chrome, objects outside the screen, thumbnails nested "
            "inside a video card, and partly visible Shorts or recommendations with no readable "
            "title. Return JSON: {\"videos\":[{\"title\":string,\"author\":string,"
            "\"published_at\":string,\"duration\":string,\"position\":string,"
            "\"bbox\":[left,top,right,bottom]}]}. Use empty strings for unknown fields. "
            "bbox is the whole card, including title and author, in 0-1000 image coordinates. "
            "Never combine text from two cards, and never invent unreadable words or dates.")},
        {"role": "user", "content": [
            {"type": "text", "text": "OCR reading aid (may mix several cards):\n" + ocr[:4500]},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
        ]},
    ], vision=True, purpose="photo identification"))
    videos = []
    seen = set()
    for raw in result.get("videos", [])[:12]:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        author = str(raw.get("author") or "").strip()
        if not title and not author:
            continue
        key = (title.casefold(), author.casefold())
        if key in seen:
            continue
        seen.add(key)
        bbox = raw.get("bbox")
        if (not isinstance(bbox, list) or len(bbox) != 4 or
                not all(isinstance(x, (int, float)) and 0 <= x <= 1000 for x in bbox) or
                bbox[0] >= bbox[2] or bbox[1] >= bbox[3]):
            bbox = None
        videos.append({"title": title, "author": author,
                       "published_at": str(raw.get("published_at") or "").strip(),
                       "duration": str(raw.get("duration") or "").strip(),
                       "position": str(raw.get("position") or "").strip(),
                       "bbox": bbox})
    if not videos:
        raise ValueError("The vision model could not identify a complete video card. Configure a vision-capable PVA_VISION_MODEL.")
    return {"videos": videos, "ocr_text": ocr}


def verify_video_matches(video: dict, candidates: list[dict]) -> list[dict]:
    """Judge search results only; the model never performs a web search."""
    if not candidates:
        return []
    evidence = {key: video.get(key, "") for key in
                ("title", "author", "published_at", "duration", "position")}
    compact = [{"index": i, **{key: candidate.get(key, "") for key in
                ("title", "author", "published_at", "duration")}}
               for i, candidate in enumerate(candidates)]
    result = _json(chat([
        {"role": "system", "content": (
            "Compare one video card identified from a photo with supplied web-search results. "
            "You cannot browse or search. Judge each result only from these fields. "
            "A similar topic is not enough: require a close title and compatible creator, "
            "duration, and date when available. Return JSON: "
            "{\"matches\":[{\"index\":integer,\"match\":boolean,"
            "\"confidence\":number,\"reason\":string}]}. Confidence is 0 to 1. "
            "Mark uncertain results false.")},
        {"role": "user", "content": json.dumps({"photo_video": evidence,
                                                   "search_results": compact}, ensure_ascii=False)},
    ], vision=True, purpose="match checking"))
    judgments = {x.get("index"): x for x in result.get("matches", [])
                 if isinstance(x, dict) and isinstance(x.get("index"), int)}
    checked = []
    for index, candidate in enumerate(candidates):
        judgment = judgments.get(index, {})
        confidence = judgment.get("confidence", 0)
        try:
            confidence = max(0.0, min(1.0, float(confidence)))
        except (ValueError, TypeError):
            confidence = 0.0
        checked.append({**candidate,
                        "verified": judgment.get("match") is True and confidence >= 0.65,
                        "confidence": confidence,
                        "reason": str(judgment.get("reason") or "No model judgment")})
    return sorted(checked, key=lambda item: (item["verified"], item["score"], item["confidence"]),
                  reverse=True)


def _load_env() -> None:
    project = Path(__file__).resolve().parents[1]
    for path in (project / ".env",):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"\''))


def chat(messages: list[dict], *, vision: bool = False, purpose: str = "other") -> str:
    _load_env()
    base = os.getenv("PVA_LLM_BASE_URL") or os.getenv("LLM_BASE_URL") or ""
    key = os.getenv("PVA_LLM_API_KEY") or os.getenv("LLM_API_KEY") or ""
    host = urlparse(base).hostname or ""
    model = ("deepseek-flash" if host == "api.deepseek.com" else
             ((os.getenv("PVA_VISION_MODEL") if vision else os.getenv("PVA_SUMMARY_MODEL")) or
              os.getenv("PVA_LLM_MODEL") or os.getenv("LLM_MODEL") or ""))
    if not (base and key and model):
        raise RuntimeError("Set PVA_LLM_BASE_URL, PVA_LLM_API_KEY and PVA_LLM_MODEL in personal_video_agent/.env")
    body = json.dumps({"model": model, "messages": messages,
                       "temperature": 0.1}).encode("utf-8")
    request = urllib.request.Request(base.rstrip("/") + "/chat/completions", data=body,
                                     headers={"Authorization": "Bearer " + key,
                                              "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            output = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"LLM HTTP {exc.code}: {exc.read(300).decode(errors='replace')}") from exc
    content = output["choices"][0]["message"]["content"]
    context = _usage_context.get()
    if context and isinstance(output.get("usage"), dict):
        db_path, video_id, finding_task_id = context
        usage_db = store.connect(db_path)
        try:
            store.record_ai_usage(
                usage_db, video_id=video_id, finding_task_id=finding_task_id,
                purpose=purpose, requested_model=model,
                response_model=str(output.get("model") or model), usage=output["usage"])
        finally:
            usage_db.close()
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("The model returned an empty answer")
    return content


def _json(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    result = json.loads(text)
    if not isinstance(result, dict):
        raise ValueError("The model must return a JSON object")
    return result


def translate(segments: list[dict], source_language: str, target_language: str,
              on_progress: Callable[[int, int], None] | None = None) -> dict[int, str]:
    if same_language(source_language, target_language):
        return {}
    translated: dict[int, str] = {}
    total = (len(segments) + 34) // 35
    for start in range(0, len(segments), 35):
        batch = segments[start:start + 35]
        payload = [{"id": s["id"], "text": s["text"]} for s in batch]
        result = _json(chat([
            {"role": "system", "content": f"Translate each text into {LANGUAGE_NAMES.get(target_language, target_language)}. Return JSON: {{\"items\":[{{\"id\":integer,\"text\":string}}]}}. Preserve every id and meaning. Do not summarize."},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ], purpose="translation"))
        items = result.get("items", [])
        found = {int(x["id"]): str(x["text"]).strip() for x in items}
        expected = {s["id"] for s in batch}
        if set(found) != expected or not all(found.values()):
            raise ValueError("Translation omitted or changed segment IDs")
        translated.update(found)
        if on_progress:
            on_progress(start // 35 + 1, total)
    return translated


def read_frame(image: Path) -> str:
    ocr = local_ocr(image)
    try:
        encoded = base64.b64encode(image.read_bytes()).decode("ascii")
        result = _json(chat([
            {"role": "system", "content": "Read this video frame. Describe only visible chart, table, diagram or slide information. Transcribe legible labels and numbers. State uncertainty. Return JSON with a 'text' string."},
            {"role": "user", "content": [{"type": "text", "text": "Extract the visual evidence. Local OCR found: " + ocr},
                                          {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + encoded}}]},
        ], vision=True, purpose="frame reading"))
        description = str(result.get("text") or "").strip()
        if any(word in description.casefold() for word in ("unavailable", "unsupported", "cannot read")):
            return ocr
        return (f"OCR text: {ocr}\nVisual description: {description}" if ocr and description
                else ocr or description)
    except Exception:
        return ocr


def summarize(segments: list[dict], frames: list[dict], *, target_language: str,
              skill_text: str, feedback: list[dict],
              on_progress: Callable[[int, int], None] | None = None) -> tuple[str, list[dict]]:
    if not segments:
        raise ValueError("A summary requires transcript segments")
    evidence_ids = {f"s:{s['id']}" for s in segments} | {
        f"f:{f['id']}" for f in frames if f["extracted_text"]}
    rows = [{"id": f"s:{s['id']}", "start": s["start"],
             "text": s.get("translated_text") or s["text"]} for s in segments]
    rows += [{"id": f"f:{f['id']}", "start": f["at_seconds"],
              "text": f["extracted_text"]} for f in frames if f["extracted_text"]]
    notes: list[dict] = []
    total_calls = (len(rows) + 64) // 65 + 1
    for start in range(0, len(rows), 65):
        batch = rows[start:start + 65]
        result = _json(chat([
            {"role": "system", "content": "Extract factual notes from video evidence. Return JSON with notes: [{text, evidence:[id]}]. Every evidence id must be copied from the input. Include useful visual facts. Do not invent missing details."},
            {"role": "user", "content": json.dumps(batch, ensure_ascii=False)},
        ], purpose="summary notes"))
        allowed = {r["id"] for r in batch}
        for note in result.get("notes", []):
            ids = [x for x in note.get("evidence", []) if x in allowed]
            if ids and str(note.get("text", "")).strip():
                notes.append({"text": str(note["text"]).strip(), "evidence": ids})
        if on_progress:
            on_progress(start // 65 + 1, total_calls)
    if not notes:
        raise ValueError("The model produced no evidence-backed notes")
    recent_feedback = [x["note"] for x in feedback[-8:] if x.get("note")]
    result = _json(chat([
        {"role": "system", "content": skill_text + "\nReturn JSON with takeaway ({text:string,evidence:[id]}), importance ({text:string,evidence:[id]}) when supported, key_points ([{text:string,evidence:[id]}]), caveats ([{text:string,evidence:[id]}]), and claims ([{text:string,evidence:[id]}]) for compatibility. Write in " + LANGUAGE_NAMES.get(target_language, target_language) + ". Every factual statement needs supplied evidence IDs, but put IDs only in the evidence arrays, never in text. User feedback: " + json.dumps(recent_feedback, ensure_ascii=False)},
        {"role": "user", "content": json.dumps(notes, ensure_ascii=False)},
    ], purpose="summary"))

    def valid(raw: object) -> dict | None:
        if not isinstance(raw, dict):
            return None
        ids = [x for x in raw.get("evidence", []) if x in evidence_ids]
        clean = re.sub(r"\s*\((?:(?:s|f):\d+(?:,\s*)?)+\)\s*$", "",
                       str(raw.get("text") or "").strip())
        return {"text": clean, "evidence": ids} if clean and ids else None

    takeaway = valid(result.get("takeaway"))
    importance = valid(result.get("importance"))
    key_points = [item for raw in (result.get("key_points") or [])
                  if (item := valid(raw))]
    caveats = [item for raw in (result.get("caveats") or [])
               if (item := valid(raw))]
    if not takeaway and not key_points:
        key_points = [item for raw in (result.get("claims") or [])
                      if (item := valid(raw))]
    claims = [item for item in ([takeaway, importance] + key_points + caveats) if item]
    if not claims:
        raise ValueError("The model returned no traced summary points")
    sections = []
    if takeaway:
        sections.append(takeaway["text"])
    if importance:
        sections.append("**Why it matters**\n" + importance["text"])
    if key_points:
        sections.append("**Key points**\n" + "\n".join("- " + x["text"] for x in key_points))
    if caveats:
        sections.append("**Limits and uncertainty**\n" +
                        "\n".join("- " + x["text"] for x in caveats))
    summary = "\n\n".join(sections)
    if on_progress:
        on_progress(total_calls, total_calls)
    return summary, claims


def classify(summary: str) -> str:
    result = _json(chat([
        {"role": "system", "content": "Classify the supplied summary into exactly one category. Return JSON {\"category\":string}. Allowed: " + ", ".join(CATEGORIES)},
        {"role": "user", "content": summary},
    ], purpose="classification"))
    category = str(result.get("category", "")).casefold().strip()
    if category not in CATEGORIES:
        raise ValueError("The model returned an unsupported category")
    return category
