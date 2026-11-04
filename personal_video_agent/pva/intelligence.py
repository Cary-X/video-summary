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

from . import llama_runtime, store, summary_router, visual
from .media import same_language


CATEGORIES = ("technology", "medicine", "finance", "politics", "religion",
              "music", "lifestyle", "sports", "health", "policy", "science",
              "education", "culture", "business", "other")
LANGUAGE_NAMES = {"en": "English", "zh-Hans": "简体中文", "zh-Hant": "繁體中文",
                  "es": "Español", "yue": "粵語（香港繁體中文口語）"}
_usage_context: ContextVar[tuple[Path, str | None, str | None] | None] = ContextVar(
    "pva_usage_context", default=None)
_local_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


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


def local_model() -> str:
    _load_env()
    return os.getenv("PVA_LOCAL_MODEL", "").strip() or "qwen3:1.7b"


class ModelOutputLimit(RuntimeError):
    """The model responded, but exhausted its output budget."""


def _local_chat(messages: list[dict], purpose: str) -> str:
    model = local_model()
    limits = {"classification": 80, "summary notes": 480, "summary": 850, "translation": 2500}
    string = {"type": "string"}
    point = {"type": "object", "required": ["text", "evidence"], "additionalProperties": False,
             "properties": {"text": string, "evidence": {"type": "array", "items": string,
                                                         "maxItems": 4}}}
    points = {"type": "array", "items": point, "maxItems": 4}
    fields = {
        "summary notes": {"notes": points},
        "summary": {"takeaway": point, "importance": point,
                    "key_points": points, "caveats": points},
        "translation": {"items": {"type": "array", "items": {
            "type": "object", "required": ["id", "text"], "properties": {
                "id": {"type": "integer"}, "text": string}}}},
        "classification": {"category": {"type": "string", "enum": list(CATEGORIES)}},
    }[purpose]
    required = ["takeaway", "key_points", "caveats"] if purpose == "summary" else list(fields)
    schema = {"type": "object", "properties": fields, "required": required,
              "additionalProperties": False}
    if summary_router.current.get().backend == "llama.cpp":
        model = llama_runtime.paths()[1].stem
        output = llama_runtime.server.complete({
            "model": model, "messages": messages, "stream": False,
            "temperature": 0.1, "max_tokens": limits[purpose],
            "chat_template_kwargs": {"enable_thinking": False},
            "response_format": {"type": "json_object", "schema": schema},
        })
        choice = output["choices"][0]
        content = choice["message"].get("content")
        _record_usage(purpose, f"llama.cpp · {model}", f"llama.cpp · {model}", output.get("usage") or {})
        if choice.get("finish_reason") == "length":
            raise ModelOutputLimit(f"Local {model} output reached its limit during {purpose}.")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError(f"Local {model} returned an empty answer during {purpose}.")
        return content
    payload = {"model": model, "messages": messages, "format": schema,
               "stream": False, "think": False, "keep_alive": "3m",
               "options": {"temperature": 0.1, "num_ctx": 8192,
                           "num_predict": limits[purpose]}}
    request = urllib.request.Request("http://127.0.0.1:11434/api/chat",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with _local_opener.open(request, timeout=240) as response:
            output = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise RuntimeError(f"Local model unavailable. Run ollama pull {model}.") from exc
        raise RuntimeError(f"Ollama HTTP {exc.code}: {exc.read(300).decode(errors='replace')}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Ollama could not finish with {model}. Check that Ollama is running locally: {exc}") from exc
    _record_usage(purpose, f"Ollama · {model}", f"Ollama · {output.get('model') or model}",
                  {"prompt_tokens": output.get("prompt_eval_count"),
                   "completion_tokens": output.get("eval_count")})
    content = output.get("message", {}).get("content")
    if output.get("done_reason") == "length":
        raise ModelOutputLimit(f"Local {model} output reached its limit during {purpose}.")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError(f"Local {model} returned an empty answer during {purpose}.")
    return content


def chat(messages: list[dict], *, vision: bool = False, purpose: str = "other") -> str:
    if purpose in {"summary notes", "summary", "translation", "classification"}:
        if vision:
            raise ValueError("The local text model accepts text only")
        route = summary_router.current.get()
        if route.backend == "api" and purpose in {"summary notes", "summary"}:
            output = summary_router.complete(route, messages)
            try:
                choice = output["choices"][0]
                content = choice["message"]["content"]
            except (TypeError, KeyError, IndexError):
                raise RuntimeError("The summary API did not return an OpenAI-compatible chat completion.") from None
            if isinstance(output.get("usage"), dict):
                _record_usage(purpose, route.model, str(output.get("model") or route.model), output["usage"])
            if choice.get("finish_reason") == "length":
                raise RuntimeError("The summary API ran out of output space. Try Batched or a model with a larger context.")
            if not isinstance(content, str) or not content.strip():
                raise RuntimeError("The summary API returned an empty answer.")
            return content
        return _local_chat(messages, purpose)
    if purpose not in {"photo identification", "match checking"}:
        raise ValueError(f"Unsupported AI step: {purpose}")
    _load_env()
    base = os.getenv("PVA_LLM_BASE_URL") or os.getenv("LLM_BASE_URL") or ""
    key = os.getenv("PVA_LLM_API_KEY") or os.getenv("LLM_API_KEY") or ""
    host = urlparse(base).hostname or ""
    model = ("deepseek-flash" if host == "api.deepseek.com" else
             ((os.getenv("PVA_VISION_MODEL") if vision else None) or
              os.getenv("PVA_LLM_MODEL") or os.getenv("LLM_MODEL") or ""))
    missing = [name for name, value in (
        ("PVA_LLM_BASE_URL", base), ("PVA_LLM_API_KEY", key),
        ("PVA_LLM_MODEL", model)) if not value]
    if missing:
        raise RuntimeError("Missing " + ", ".join(missing) +
                           " in personal_video_agent/.env. Paste your DeepSeek key "
                           "after PVA_LLM_API_KEY= to enable AI calls.")
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
    if isinstance(output.get("usage"), dict):
        _record_usage(purpose, model, str(output.get("model") or model), output["usage"])
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("The model returned an empty answer")
    return content


def _record_usage(purpose: str, model: str, response_model: str, usage: dict) -> None:
    context = _usage_context.get()
    if context:
        db_path, video_id, finding_task_id = context
        usage_db = store.connect(db_path)
        try:
            store.record_ai_usage(
                usage_db, video_id=video_id, finding_task_id=finding_task_id,
                purpose=purpose, requested_model=model,
                response_model=response_model, usage=usage)
        finally:
            usage_db.close()


def _json(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    result = json.loads(text)
    if not isinstance(result, dict):
        raise ValueError("The model must return a JSON object")
    return result


def translate(segments: list[dict], source_language: str, target_language: str,
              on_progress: Callable[[int, int], None] | None = None, *,
              on_batch: Callable[[dict[int, str]], None] | None = None) -> dict[int, str]:
    if same_language(source_language, target_language):
        return {}
    language = LANGUAGE_NAMES.get(target_language, target_language)

    def recover(batch: list[dict]) -> dict[int, str]:
        # Short local labels are easier to copy. SQLite IDs never come from the model.
        payload = [{"id": index, "text": row["text"]}
                   for index, row in enumerate(batch, start=1)]
        try:
            result = _json(chat([
                {"role": "system", "content": f"Translate every input item into {language}. "
                 'Return JSON: {"items":[{"id":integer,"text":string}]}. '
                 "Copy the input ID for each translation. Keep items separate. Do not summarize."},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ], purpose="translation"))
            items = result.get("items")
        except (ValueError, ModelOutputLimit):
            items = None
        found: dict[int, str] = {}
        if isinstance(items, list):
            # A single result has only one possible source segment.
            if len(batch) == len(items) == 1 and isinstance(items[0], dict):
                text = items[0].get("text")
                if isinstance(text, str) and text.strip():
                    return {batch[0]["id"]: text.strip()}
            seen: set[int] = set()
            for item in items:
                if not isinstance(item, dict):
                    continue
                index, text = item.get("id"), item.get("text")
                if type(index) is not int or not 1 <= index <= len(batch):
                    # Unexpected labels may indicate the whole batch was renumbered.
                    found.clear()
                    break
                if index in seen:
                    found.pop(index, None)
                elif isinstance(text, str) and text.strip():
                    found[index] = text.strip()
                seen.add(index)
        recovered = {batch[index - 1]["id"]: text for index, text in found.items()}
        missing = [row for index, row in enumerate(batch, start=1) if index not in found]
        if missing and len(batch) > 1:
            split = (len(missing) + 1) // 2
            recovered.update(recover(missing[:split]))
            if missing[split:]:
                recovered.update(recover(missing[split:]))
        return recovered

    translated: dict[int, str] = {}
    batch_size = 24
    for start in range(0, len(segments), batch_size):
        batch = segments[start:start + batch_size]
        recovered = recover(batch)
        if on_batch and recovered:
            on_batch(recovered)
        translated.update(recovered)
        if on_progress:
            on_progress(start + len(batch), len(segments))
    return translated


def read_frame(image: Path, *, on_progress=None, on_warning=None) -> str:
    _load_env()
    context = _usage_context.get()
    db_path, video_id, finding_task_id = context or (visual.ROOT / "data" / "personal.db", None, None)
    try:
        result = visual.read(image, db_path=db_path, video_id=video_id,
                             finding_task_id=finding_task_id, on_progress=on_progress)
    except RuntimeError as exc:
        if on_warning:
            on_warning(f"Local visual reading failed: {exc}")
        ocr = local_ocr(image)
        if ocr:
            return "RapidOCR text only; chart structure and visual details are unverified.\n" + ocr
        raise
    return f"Local {result['model']} visual evidence:\n" + result["text"]


def summarize(segments: list[dict], frames: list[dict], *, target_language: str,
              skill_text: str, feedback: list[dict],
              on_progress: Callable[[int, int], None] | None = None,
              on_warning: Callable[[str], None] | None = None) -> tuple[str, list[dict]]:
    if not segments:
        raise ValueError("A summary requires transcript segments")
    rows = [{"id": f"s:{s['id']}", "start": s["start"],
             "text": s.get("translated_text") or s["text"]} for s in segments]
    rows += [{"id": f"f:{f['id']}", "start": f["at_seconds"],
              "text": f["extracted_text"]} for f in frames if f["extracted_text"]]

    def extract_notes(batch: list[dict], allowed: set[str]) -> list[dict]:
        result = _json(chat([
            {"role": "system", "content": "Extract up to four brief factual notes from video evidence. Use one sentence per note. Return JSON with notes: [{text, evidence:[id]}]. Copy one or two evidence IDs per note from the input id or evidence fields. Include useful visual facts. Do not invent missing details."},
            {"role": "user", "content": json.dumps(batch, ensure_ascii=False)},
        ], purpose="summary notes"))
        valid_notes = []
        for note in result.get("notes", [])[:4]:
            ids = [x for x in note.get("evidence", []) if x in allowed]
            if ids and str(note.get("text", "")).strip():
                valid_notes.append({"text": str(note["text"]).strip(), "evidence": ids})
        return valid_notes

    route = summary_router.current.get()
    input_size = sum(len(row["text"]) for row in rows)
    short_input = len(rows) <= 65 and input_size <= 6000
    direct_fits = input_size <= 60000 and len(rows) <= 600 if route.backend == "api" else short_input
    if route.method == "direct" and not direct_fits and on_warning:
        on_warning("Transcript exceeds the single-pass limit. Continuing with batched summarization.")
    if (route.method == "direct" and direct_fits) or (route.method == "auto" and short_input):
        summary_input = rows
        evidence_ids = {row["id"] for row in rows}
        total_calls = 1
    else:
        notes: list[dict] = []
        total_calls = (len(rows) + 64) // 65 + 1
        for start in range(0, len(rows), 65):
            batch = rows[start:start + 65]
            notes.extend(extract_notes(batch, {row["id"] for row in batch}))
            if on_progress:
                on_progress(start // 65 + 1, total_calls)
        while len(notes) > 60:
            condensed = []
            for start in range(0, len(notes), 30):
                batch = notes[start:start + 30]
                condensed.extend(extract_notes(batch, {x for note in batch for x in note["evidence"]}))
            notes = condensed
        if not notes:
            raise ValueError("The model produced no evidence-backed notes")
        summary_input = notes
        evidence_ids = {evidence_id for note in notes for evidence_id in note["evidence"]}
    recent_feedback = [x["note"] for x in feedback[-8:] if x.get("note")]
    result = _json(chat([
        {"role": "system", "content": skill_text + "\nReturn compact JSON. Use one sentence for the takeaway, up to four short key points, and only material caveats. Each point is {text:string,evidence:[id]}. Write in " + LANGUAGE_NAMES.get(target_language, target_language) + ". Every factual statement needs supplied evidence IDs, but put IDs only in the evidence arrays, never in text. User feedback: " + json.dumps(recent_feedback, ensure_ascii=False)},
        {"role": "user", "content": json.dumps(summary_input, ensure_ascii=False)},
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
        {"role": "system", "content": (
            "Classify the supplied summary into exactly one category. Return JSON "
            "{\"category\":string}. Allowed: " + ", ".join(CATEGORIES) + ". "
            "Use medicine for diagnosis or treatment, health for wellness or "
            "general health, politics for elections or political actors, and "
            "policy for government rules or programs. Choose the dominant topic.")},
        {"role": "user", "content": summary},
    ], purpose="classification"))
    category = str(result.get("category", "")).casefold().strip()
    if category not in CATEGORIES:
        raise ValueError("The model returned an unsupported category")
    return category
