"""Source discovery, captions, audio transcription, and selective frame capture."""
from __future__ import annotations

import re
import json
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse


_asr_lock = threading.Lock()


VISUAL_CUE = re.compile(
    r"\b(chart|graph|figure|diagram|slide|screen|picture|image|table|flowchart|"
    r"look at|as you can see|shown here)\b|图表|图中|画面|表格|流程图|如图|幻灯片",
    re.IGNORECASE,
)


def same_language(source: str, target: str) -> bool:
    """Keep Chinese scripts and Cantonese distinct; ignore ordinary region tags."""
    def normalized(code: str) -> str:
        value = code.casefold().replace("_", "-")
        if value.startswith(("yue", "zh-yue")):
            return "yue"
        if value.startswith(("zh-hant", "zh-tw", "zh-hk", "zh-mo")):
            return "zh-hant"
        if value.startswith(("zh-hans", "zh-cn", "zh-sg")):
            return "zh-hans"
        if value.startswith("zh"):
            return "zh-unknown"
        return value.split("-")[0]
    return normalized(source) == normalized(target)


def valid_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Enter a public http(s) video link")
    return url.strip()


def _ydl(options: dict, *, cookie_browser: str = ""):
    try:
        from yt_dlp import YoutubeDL
    except ImportError as exc:
        raise RuntimeError("Install yt-dlp: pip install -r requirements.txt") from exc
    auth = {}
    if cookie_browser:
        auth["cookiesfrombrowser"] = (cookie_browser.casefold(), None, None, None)
    return YoutubeDL({"quiet": True, "no_warnings": True, "noprogress": True,
                      "noplaylist": True,
                      **auth, **options})


def _bot_challenge(exc: Exception) -> bool:
    message = str(exc).casefold()
    return "not a bot" in message or "sign in to confirm" in message


def _cookie_browsers(choice: str) -> list[str]:
    """Try the selected browser first, with Firefox as the first auto choice."""
    if not choice:
        return []
    order = ["firefox", "chrome", "edge"]
    selected = choice.casefold()
    if selected == "auto":
        return order
    if selected not in order:
        raise ValueError("Unsupported YouTube cookie browser")
    return [selected] + [browser for browser in order if browser != selected]


def _cookie_retryable(exc: Exception) -> bool:
    message = str(exc).casefold()
    return _bot_challenge(exc) or any(phrase in message for phrase in (
        "failed to load cookies", "could not find firefox cookies",
        "could not find chrome cookies", "could not find edge cookies",
        "failed to decrypt", "cookie database is locked", "http error 403"))


def _with_cookie_fallback(operation, choice: str, *, retry_ssl: bool = False):
    """Retry access problems across local browser profiles without saving cookies."""
    browsers = _cookie_browsers(choice)
    tried = []
    try:
        return operation("")
    except Exception as exc:
        if retry_ssl and "unexpected_eof_while_reading" in str(exc).casefold():
            try:
                return operation("")
            except Exception as second:
                exc = second
        if not browsers or not _cookie_retryable(exc):
            raise exc
    for browser in browsers:
        tried.append(browser)
        try:
            return operation(browser)
        except Exception as exc:
            if not _cookie_retryable(exc):
                raise
            last_error = exc
    raise RuntimeError(
        "YouTube access failed after trying " + ", ".join(tried) +
        ". Sign in to YouTube in Firefox or choose another browser; last error: " +
        str(last_error)) from last_error


def metadata(url: str, *, cookie_browser: str = "") -> dict:
    valid_url(url)
    def extract(browser: str):
        with _ydl({"skip_download": True}, cookie_browser=browser) as ydl:
            info = ydl.extract_info(url, download=False)
        return info
    info = _with_cookie_fallback(extract, cookie_browser)
    if not info or info.get("_type") == "playlist":
        raise ValueError("The link did not resolve to one video")
    return {
        "url": info.get("webpage_url") or url,
        "title": info.get("title") or "Untitled video",
        "author": info.get("uploader") or info.get("channel") or "",
        "published_at": info.get("upload_date") or "",
        "duration": info.get("duration"),
        "raw": info,
    }


def search_candidates(title: str, author: str = "", published_at: str = "",
                      photo_text: str = "", limit: int = 5,
                      cookie_browser: str = "") -> list[dict]:
    query = " ".join(x for x in (title, author, photo_text[:100]) if x).strip()
    if not query:
        raise ValueError("A title or readable photo text is needed to find the video")
    search = f"ytsearch{max(1, min(limit, 10))}:{query}"
    options = {"extract_flat": "in_playlist", "skip_download": True}
    def extract(browser: str):
        with _ydl(options, cookie_browser=browser) as ydl:
            results = ydl.extract_info(search, download=False)
        return results
    results = _with_cookie_fallback(extract, cookie_browser)
    entries = (results or {}).get("entries") or []
    title_terms = set(re.findall(r"\w+", title.casefold()))
    author_terms = set(re.findall(r"\w+", author.casefold()))
    candidates = []
    for entry in entries:
        url = entry.get("webpage_url") or entry.get("url") or ""
        if not url.startswith("http") and entry.get("id"):
            url = "https://www.youtube.com/watch?v=" + entry["id"]
        if not url.startswith("http"):
            continue
        name = entry.get("title") or ""
        creator = entry.get("uploader") or entry.get("channel") or ""
        duration_seconds = entry.get("duration")
        duration = (f"{int(duration_seconds) // 60}:{int(duration_seconds) % 60:02d}"
                    if isinstance(duration_seconds, (int, float)) else "")
        words = set(re.findall(r"\w+", name.casefold()))
        creator_words = set(re.findall(r"\w+", creator.casefold()))
        score = len(title_terms & words) / max(1, len(title_terms))
        score += 0.25 * len(author_terms & creator_words) / max(1, len(author_terms))
        if published_at and entry.get("upload_date") == re.sub(r"\D", "", published_at)[:8]:
            score += 0.25
        candidates.append({"url": url, "title": name, "author": creator,
                           "published_at": entry.get("upload_date") or "",
                           "duration": duration,
                           "score": round(score, 2)})
    return sorted(candidates, key=lambda item: item["score"], reverse=True)


def photo_ocr(path: Path) -> str:
    from .intelligence import local_ocr
    return local_ocr(path)


def _parse_vtt(raw: str) -> list[dict]:
    def seconds(value: str) -> float:
        parts = value.replace(",", ".").split(":")
        return sum(float(piece) * 60**i for i, piece in enumerate(reversed(parts)))

    blocks = re.split(r"\n\s*\n", raw.replace("\r", ""))
    segments = []
    for block in blocks:
        lines = block.splitlines()
        timing = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing is None:
            continue
        match = re.search(r"(\d{1,2}:)?\d{2}:\d{2}[.,]\d{3}\s*-->\s*((\d{1,2}:)?\d{2}:\d{2}[.,]\d{3})", lines[timing])
        if not match:
            continue
        start = seconds(match.group(0).split("-->")[0].strip())
        end = seconds(match.group(2))
        words = " ".join(re.sub(r"<[^>]+>", "", line).strip()
                         for line in lines[timing + 1:] if not line.startswith("NOTE"))
        words = re.sub(r"\s+", " ", words).strip()
        if words and end > start and (not segments or segments[-1]["text"] != words):
            segments.append({"start": start, "end": end, "text": words})
    return segments


def captions(info: dict, *, preferred_language: str = "en") -> tuple[list[dict], str] | None:
    """Prefer the requested language instead of the first auto-translation track."""
    preferred = preferred_language.casefold()
    original = str(info.get("language") or "").casefold()
    choices = []
    for source_rank, source in enumerate(("subtitles", "automatic_captions")):
        for language, formats in (info.get(source) or {}).items():
            track = next((x for x in formats if x.get("ext") == "vtt" and x.get("url")), None)
            if track is None:
                continue
            language_rank = (0 if language.casefold() == preferred else
                             1 if same_language(language, preferred_language) else
                             2 if original and same_language(language, original) else 3)
            choices.append((language_rank, source_rank, language, track["url"]))
    for _, _, language, url in sorted(choices):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(request, timeout=25) as response:
                segments = _parse_vtt(response.read().decode("utf-8-sig", errors="replace"))
            if segments:
                return segments, language
        except (OSError, UnicodeError):
            continue
    return None


def download_video(url: str, directory: Path, *, cookie_browser: str = "",
                   purpose: str = "asr") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    template = str(directory / "source.%(ext)s")
    if purpose not in {"asr", "visual"}:
        raise ValueError("Download purpose must be asr or visual")
    options = {"outtmpl": template,
               "format": ("bestaudio/best" if purpose == "asr" else
                          "bestvideo[height<=480]/best[height<=480]/bestvideo/best"),
               "merge_output_format": "mp4", "retries": 3,
               "fragment_retries": 3, "socket_timeout": 20,
               "http_chunk_size": 1048576}
    def fetch(browser: str):
        with _ydl(options, cookie_browser=browser) as ydl:
            ydl.download([url])
    _with_cookie_fallback(fetch, cookie_browser, retry_ssl=True)
    files = [x for x in directory.glob("source.*") if x.is_file() and not x.name.endswith(".part")]
    if not files:
        raise RuntimeError("The video could not be downloaded")
    return max(files, key=lambda p: p.stat().st_size)


def transcribe(video: Path, *, on_progress=None,
               stall_timeout: float = 180) -> tuple[list[dict], str]:
    """Serialize local ASR, with cancellation, progress, and a bounded stall wait."""
    def report(fraction: float, detail: str) -> None:
        if on_progress:
            on_progress(fraction, detail)

    while not _asr_lock.acquire(timeout=1):
        report(0, "Waiting for the other local ASR task")
    process = None
    try:
        report(0, "Starting local ASR worker")
        messages = queue.Queue()
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(
                [sys.executable, "-u", "-m", "pva.asr_worker", str(video.resolve())],
                cwd=str(Path(__file__).resolve().parent.parent), stdout=subprocess.PIPE,
                stderr=errors, text=True, encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

            def read_messages():
                try:
                    for line in process.stdout:
                        messages.put(line)
                finally:
                    messages.put(None)

            reader = threading.Thread(target=read_messages, daemon=True)
            reader.start()
            last_progress = time.monotonic()
            fraction, detail = 0, "Starting ASR"
            while True:
                try:
                    raw = messages.get(timeout=2)
                except queue.Empty:
                    elapsed = time.monotonic() - last_progress
                    if elapsed > stall_timeout:
                        raise RuntimeError(f"ASR stalled for {stall_timeout:.0f}s during: {detail}. Retry processing.")
                    report(fraction, f"{detail} · waiting {elapsed:.0f}s")
                    continue
                if raw is None:
                    errors.seek(0)
                    raise RuntimeError("ASR worker stopped: " + errors.read().decode("utf-8", "replace")[-1000:])
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if "error" in message:
                    raise RuntimeError(message["error"])
                if "result" in message:
                    result = message["result"]
                    report(1, "Local transcript complete")
                    return result["segments"], result["language"]
                fraction, detail = message["progress"], message["detail"]
                last_progress = time.monotonic()
                report(fraction, detail)
    finally:
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            process.stdout.close()
        _asr_lock.release()


def capture_frame(video: Path, at_seconds: float, output: Path) -> Path:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is needed for chart and figure screenshots")
    output.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss",
                             str(max(0, at_seconds)), "-i", str(video), "-frames:v", "1",
                             str(output)], capture_output=True, text=True, timeout=90)
    if result.returncode or not output.is_file():
        raise RuntimeError(f"Frame capture failed: {result.stderr[-300:]}")
    return output
