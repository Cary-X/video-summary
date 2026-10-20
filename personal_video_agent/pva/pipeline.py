"""One complete ingest job; every stage is recorded in the personal database."""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
from collections.abc import Callable
from pathlib import Path

from . import intelligence, media, store


def _visual_cues(rows: list[dict], max_frames: int = 8) -> list[dict]:
    """Spread screenshots across a video instead of sampling repeated captions."""
    candidates = [row for row in rows if media.VISUAL_CUE.search(
        row["text"] + " " + (row.get("translated_text") or ""))]
    if not candidates:
        return []
    selected = [candidates[0]]
    remaining = candidates[1:]
    while remaining and len(selected) < max_frames:
        next_row = max(remaining, key=lambda row: min(
            abs(row["start"] - chosen["start"]) for chosen in selected))
        distance = min(abs(next_row["start"] - chosen["start"]) for chosen in selected)
        if distance < 8:
            break
        selected.append(next_row)
        remaining.remove(next_row)
    return sorted(selected, key=lambda row: row["start"])


def process(db: sqlite3.Connection, video_id: str, *, asset_dir: Path,
            skill_file: Path, refresh: bool = False, cookie_browser: str = "",
            on_progress: Callable[[str, float, str], None] | None = None) -> dict:
    video = db.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
    if video is None:
        raise KeyError(video_id)
    if video["status"] == "ready" and not refresh:
        return store.detail(db, video_id)
    if video["status"] == "ready" and refresh:
        return resummarize(db, video_id, skill_file=skill_file)
    asset_dir.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="ingest-", dir=asset_dir))
    movie: Path | None = None

    def stage(name: str, detail: str, fraction: float) -> None:
        with db:
            store.event(db, video_id, name, detail)
        if on_progress:
            on_progress(name, fraction, detail)

    try:
        with db:
            db.execute("UPDATE videos SET status='processing',error=NULL WHERE id=?", (video_id,))
        stage("metadata", "Resolving video metadata", 0.04)
        source = media.metadata(video["url"], cookie_browser=cookie_browser)
        with db:
            db.execute("""UPDATE videos SET title=?,author=?,published_at=?,duration=? WHERE id=?""",
                       (source["title"] or video["title"], source["author"] or video["author"],
                        video["published_at"] if " " in video["published_at"] else
                        source["published_at"] or video["published_at"],
                        source["duration"], video_id))
        stage("captions", f"Checking captions in {video['target_language']} first", 0.10)
        caption_result = media.captions(source["raw"], preferred_language=video["target_language"])
        if caption_result:
            segments, language = caption_result
            transcript_source = "captions"
        else:
            stage("download", "Downloading media for local ASR", 0.16)
            movie = media.download_video(video["url"], scratch / "audio",
                                         cookie_browser=cookie_browser, purpose="asr")
            stage("asr", "Transcribing audio locally", 0.30)
            segments, language = media.transcribe(movie)
            transcript_source = "asr"
            movie = None  # Audio-only media cannot supply screenshots.
        store.replace_segments(db, video_id, segments)
        with db:
            db.execute("UPDATE videos SET language=?,transcript_source=? WHERE id=?",
                       (language, transcript_source, video_id))
        segment_rows = [dict(x) for x in db.execute(
            "SELECT * FROM segments WHERE video_id=? ORDER BY seq", (video_id,))]
        target = video["target_language"]
        if not media.same_language(language, target):
            stage("translation", f"Translating {language} to {target}", 0.45)
            translated = intelligence.translate(
                segment_rows, language, target,
                on_progress=(lambda done, total: on_progress(
                    "translation", 0.45 + 0.20 * done / total,
                    f"Translated batch {done}/{total}") if on_progress else None))
            with db:
                db.executemany("UPDATE segments SET translated_text=? WHERE id=?",
                               [(text, segment_id) for segment_id, text in translated.items()])
            for row in segment_rows:
                row["translated_text"] = translated[row["id"]]
        else:
            stage("translation", f"Skipped: transcript is already {target}", 0.65)
        cues = _visual_cues(segment_rows)
        frames: list[dict] = []
        if cues:
            stage("visual", f"Inspecting {len(cues)} visual references", 0.66)
            try:
                if movie is None:
                    movie = media.download_video(video["url"], scratch / "visual",
                                                 cookie_browser=cookie_browser,
                                                 purpose="visual")
            except Exception as exc:
                stage("visual warning", f"Screenshots skipped: {exc}", 0.78)
            if movie is not None:
                for index, row in enumerate(cues):
                    try:
                        image_path = asset_dir / "frames" / video_id / f"{row['seq']:05d}.jpg"
                        media.capture_frame(movie, (row["start"] + row["end"]) / 2, image_path)
                        extracted = intelligence.read_frame(image_path)
                        with db:
                            cursor = db.execute("""INSERT INTO frames(video_id,segment_id,at_seconds,image_path,extracted_text)
                              VALUES (?,?,?,?,?)""", (video_id, row["id"], row["start"],
                                                     str(image_path), extracted))
                        frames.append({"id": cursor.lastrowid, "segment_id": row["id"],
                                       "at_seconds": row["start"], "image_path": str(image_path),
                                       "extracted_text": extracted})
                    except Exception as exc:
                        stage("visual warning", f"Frame at {row['start']:.0f}s skipped: {exc}",
                              0.66 + 0.12 * (index + 1) / len(cues))
                    if on_progress:
                        on_progress("visual", 0.66 + 0.12 * (index + 1) / len(cues),
                                    f"Inspected frame {index + 1}/{len(cues)}")
        else:
            stage("visual", "No visual references found", 0.78)
        stage("summary", "Writing grounded summary", 0.79)
        feedback = [dict(x) for x in db.execute("SELECT * FROM feedback ORDER BY id")]
        summary, claims = intelligence.summarize(
            segment_rows, frames, target_language=target,
            skill_text=skill_file.read_text(encoding="utf-8"), feedback=feedback,
            on_progress=(lambda done, total: on_progress(
                "summary", 0.79 + 0.17 * done / total,
                f"Summary call {done}/{total}") if on_progress else None))
        stage("classification", "Categorizing from the summary", 0.97)
        category = intelligence.classify(summary)
        store.save_result(db, video_id, summary, category, claims)
        if on_progress:
            on_progress("ready", 1.0, "Summary and evidence saved")
        return store.detail(db, video_id)
    except Exception as exc:
        with db:
            db.execute("UPDATE videos SET status='failed',error=? WHERE id=?", (str(exc), video_id))
            store.event(db, video_id, "failed", f"{type(exc).__name__}: {exc}")
        raise
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def resummarize(db: sqlite3.Connection, video_id: str, *, skill_file: Path) -> dict:
    video = store.detail(db, video_id)
    if not video["segments"]:
        raise ValueError("No stored transcript exists for this video")
    with db:
        store.event(db, video_id, "summary", "Refreshing from stored evidence and feedback")
    feedback_rows = [dict(x) for x in db.execute("SELECT * FROM feedback ORDER BY id")]
    summary, claims = intelligence.summarize(
        video["segments"], video["frames"], target_language=video["target_language"],
        skill_text=skill_file.read_text(encoding="utf-8"), feedback=feedback_rows)
    category = intelligence.classify(summary)
    store.save_result(db, video_id, summary, category, claims)
    return store.detail(db, video_id)


def repair_visuals(db: sqlite3.Connection, video_id: str, *, asset_dir: Path,
                   skill_file: Path, cookie_browser: str = "",
                   on_progress: Callable[[str, float, str], None] | None = None) -> dict:
    """Retry missing screenshots from stored captions, then refresh the summary."""
    video = store.detail(db, video_id)
    if video["status"] != "ready":
        raise ValueError("Finish the video summary before retrying screenshots")
    existing = {frame["segment_id"] for frame in video["frames"]}
    cues = [row for row in _visual_cues(video["segments"]) if row["id"] not in existing]
    if not cues:
        raise ValueError("No missing visual references remain")
    asset_dir.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="visual-retry-", dir=asset_dir))
    try:
        if on_progress:
            on_progress("visual download", 0.1, "Downloading a small visual copy")
        movie = media.download_video(video["url"], scratch, cookie_browser=cookie_browser,
                                     purpose="visual")
        for index, row in enumerate(cues):
            image_path = asset_dir / "frames" / video_id / f"{row['seq']:05d}.jpg"
            try:
                media.capture_frame(movie, (row["start"] + row["end"]) / 2, image_path)
                extracted = intelligence.read_frame(image_path)
                with db:
                    db.execute("""INSERT OR IGNORE INTO frames
                      (video_id,segment_id,at_seconds,image_path,extracted_text)
                      VALUES (?,?,?,?,?)""", (video_id, row["id"], row["start"],
                                           str(image_path), extracted))
            except Exception as exc:
                with db:
                    store.event(db, video_id, "visual warning",
                                f"Frame at {row['start']:.0f}s skipped: {exc}")
            if on_progress:
                on_progress("visual", 0.1 + 0.7 * (index + 1) / len(cues),
                            f"Inspected frame {index + 1}/{len(cues)}")
        if on_progress:
            on_progress("summary", 0.85, "Refreshing summary with visual evidence")
        result = resummarize(db, video_id, skill_file=skill_file)
        if on_progress:
            on_progress("ready", 1.0, "Screenshots and summary updated")
        return result
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def feedback(db: sqlite3.Connection, video_id: str, rating: int, note: str = "") -> None:
    if not 1 <= rating <= 5:
        raise ValueError("Rating must be from 1 to 5")
    with db:
        db.execute("INSERT INTO feedback(video_id,at,rating,note) VALUES (?,?,?,?)",
                   (video_id, store.now(), rating, note.strip()))
        store.event(db, video_id, "feedback", f"Rating {rating}/5")


def measures(db: sqlite3.Connection) -> dict:
    return {
        "videos": db.execute("SELECT count(*) FROM videos").fetchone()[0],
        "ready": db.execute("SELECT count(*) FROM videos WHERE status='ready'").fetchone()[0],
        "failed": db.execute("SELECT count(*) FROM videos WHERE status='failed'").fetchone()[0],
        "visual": db.execute("SELECT count(DISTINCT video_id) FROM frames").fetchone()[0],
        "feedback_count": db.execute("SELECT count(*) FROM feedback").fetchone()[0],
        "average_rating": db.execute("SELECT round(avg(rating),2) FROM feedback").fetchone()[0],
        "traced_claims": db.execute("SELECT count(*) FROM claims").fetchone()[0],
    }
