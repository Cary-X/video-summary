"""Shared intake for the desktop drop window and the browser."""
from pathlib import Path
import hashlib
import shutil
import uuid
from urllib.parse import urlsplit

from . import media, store, tasks, uploads, web_sources
from .summary_router import SummaryRoute

PHOTOS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_HOSTS = {"youtu.be", "youtube.com", "bilibili.com", "b23.tv", "tiktok.com",
               "douyin.com", "vimeo.com", "dailymotion.com", "dai.ly", "twitch.tv"}


def add(db, data, source, automatic, method, override="auto"):
    if method not in {"auto", "direct", "batched"}:
        raise ValueError("Choose a summary method.")
    parsed = urlsplit(source)
    if parsed.scheme in {"http", "https"}:
        web_sources.validate_url(source, resolve=False)
        host = parsed.hostname.lower().rstrip(".")
        video = any(host == item or host.endswith("." + item) for item in VIDEO_HOSTS)
        kind = override if override in {"video", "webpage"} else ("video" if video else "webpage")
        payload = source
    else:
        path = Path(source)
        suffix = path.suffix.lower()
        if not path.is_file() or (suffix not in PHOTOS and suffix[1:] not in uploads.EXTENSIONS):
            raise ValueError("Drop a URL, screenshot, or supported audio/document file.")
        if path.stat().st_size > 200 * 1024 * 1024:
            raise ValueError("Files must be 200 MB or smaller.")
        folder = data / "desktop-inbox" / uuid.uuid4().hex
        folder.mkdir(parents=True)
        copy = folder / path.name
        shutil.copyfile(path, copy)
        payload = str(copy)
        kind = "photo" if suffix in PHOTOS else "file"
    with db:
        db.execute("""INSERT INTO desktop_inbox(kind,source,automatic,method,created_at)
                      VALUES(?,?,?,?,?)""", (kind, payload, automatic, method, store.now()))
    return kind


def submit(db, data, item, *, approve=False):
    with db:
        claimed = db.execute("""UPDATE desktop_inbox SET status='sending',error=NULL
          WHERE id=? AND (status='pending' OR (status='failed' AND ?))""", (item["id"], approve)).rowcount
    if not claimed:
        return False
    prefs = dict(db.execute("SELECT key,value FROM preferences"))
    language = prefs.get("summary_language", "en")
    route = SummaryRoute(backend=prefs.get("summary_backend", "llama.cpp"), method=item["method"],
                         base_url=prefs.get("summary_base_url", ""), model=prefs.get("summary_model", ""))
    kind, source = item["kind"], item["source"]
    try:
        if kind == "video":
            url = media.valid_url(source)
            route.register()
            video_id = store.add_video(db, url, title=url, target_language=language)
            if not db.execute("""SELECT 1 FROM processing_tasks
                WHERE video_id=? AND status IN ('queued','running')""", (video_id,)).fetchone():
                tasks.enqueue_processing_video(db, video_id, summary_route=route)
        elif kind == "webpage":
            web_sources.enqueue(db, source, language, route)
        elif kind == "file":
            path = Path(source)
            uploads.save(db, data, path.name, path.read_bytes(), language, route)
        else:
            path = Path(source)
            content = path.read_bytes()
            folder = data / "photos"
            folder.mkdir(exist_ok=True)
            photo = folder / (hashlib.sha256(content).hexdigest() + path.suffix.lower())
            photo.write_bytes(content)
            tasks.enqueue_find(db, "photo", str(photo), photo_path=str(photo), card={
                "_desktop_summary": {"automatic": bool(item["automatic"]), "method": item["method"]}})
    except Exception as exc:
        with db:
            db.execute("UPDATE desktop_inbox SET status='failed',error=? WHERE id=?", (str(exc), item["id"]))
        return False
    with db:
        db.execute("DELETE FROM desktop_inbox WHERE id=?", (item["id"],))
    if kind in {"file", "photo"}:
        path = Path(source)
        try:
            path.unlink(missing_ok=True)
            path.parent.rmdir()
        except OSError:
            pass  # The processing task already owns its copy.
    return True
