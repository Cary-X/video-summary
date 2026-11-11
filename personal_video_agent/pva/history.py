"""Keep processing outcomes after purging the corresponding library item."""
from pathlib import Path
import re


def page(db, outcome, offset, limit):
    where = "WHERE h.outcome=?" if outcome else ""
    params = (outcome,) if outcome else ()
    count = db.execute(f"SELECT COUNT(*) FROM processing_history h {where}", params).fetchone()[0]
    rows = db.execute(f"""
        SELECT h.*, v.status AS item_status, v.title
        FROM processing_history h LEFT JOIN videos v ON v.id=h.video_id
        {where} ORDER BY h.at DESC,h.id DESC LIMIT ? OFFSET ?
    """, (*params, limit, offset)).fetchall()
    return rows, count


def reprocess(db, history_id, language, *, summary_route):
    from . import media, store, tasks, web_sources

    record = db.execute("SELECT * FROM processing_history WHERE id=?", (history_id,)).fetchone()
    if record is None:
        raise ValueError("This history record has been deleted.")
    kind = record["source_kind"]
    online = kind in {"video", "webpage"}
    if online:
        item = db.execute("SELECT id,status FROM videos WHERE url=?", (record["source"],)).fetchone()
    else:
        item = db.execute("SELECT id,status FROM videos WHERE id=?", (record["video_id"],)).fetchone()
    if item:
        active = db.execute("""
            SELECT 1 FROM processing_tasks WHERE video_id=? AND status IN ('queued','running')
        """, (item["id"],)).fetchone()
        if active:
            raise ValueError("This source already has a task in Processing.")
        if item["status"] == "ready":
            return tasks.enqueue_processing_video(db, item["id"], mode="resummarize", summary_route=summary_route)
        if not online:
            original = db.execute("SELECT path FROM uploaded_sources WHERE video_id=?", (item["id"],)).fetchone()
            if not original or not original["path"] or not Path(original["path"]).is_file():
                raise ValueError("Upload the original file again in Add sources.")
        return tasks.enqueue_processing_video(db, item["id"], summary_route=summary_route)
    if kind == "webpage":
        return web_sources.enqueue(db, record["source"], language, summary_route)
    if kind == "video":
        summary_route.register()
        url = media.valid_url(record["source"])
        video_id = store.add_video(db, url, title=url, target_language=language)
        return tasks.enqueue_processing_video(db, video_id, summary_route=summary_route)
    raise ValueError("Upload the original file again in Add sources.")


def _owned_files(root, video_id, paths, original):
    allowed = [(root / name).resolve() for name in ("uploads", "webpages", "photos", "frames")]
    paths = [Path(path).resolve() for path in paths if path]
    files = {path for path in paths if path.is_relative_to(root) and
             any(path.is_relative_to(folder) and path != folder for folder in allowed)}
    frame_dir = (root / "frames" / video_id).resolve()
    if frame_dir.parent == (root / "frames").resolve() and frame_dir.is_relative_to(root):
        files.update(path for path in frame_dir.glob("*.jpg") if not path.is_symlink())
    if original:
        path = Path(original).resolve()
        if path in files and path.parent in {(root / "uploads").resolve(), (root / "webpages").resolve()}:
            pattern = re.compile(re.escape(path.stem) + r"-(?:page|image)-\d+\.png$")
            files.update(sibling for sibling in path.parent.glob(path.stem + "-*.png")
                         if not sibling.is_symlink() and pattern.fullmatch(sibling.name))
    return files, frame_dir


def remove_item(db, video_id, asset_dir, active_tasks):
    """Archive identity and outcomes, then purge the local item and unshared files."""
    with db:
        db.execute("BEGIN IMMEDIATE")
        video = db.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
        if video is None:
            return []
        processing = db.execute("SELECT id,status FROM processing_tasks WHERE video_id=?", (video_id,)).fetchall()
        if any(row["id"] in active_tasks or row["status"] in {"queued", "running"} for row in processing):
            raise ValueError("This item has an active task. Wait for it to finish or remove it from Processing first.")
        original = db.execute("SELECT path FROM uploaded_sources WHERE video_id=?", (video_id,)).fetchone()
        original = original["path"] if original else None
        finding = db.execute("SELECT id,photo_path,parent_id FROM finding_tasks WHERE video_id=?",
                             (video_id,)).fetchall()
        paths = [original, video["source_photo"], *(row["photo_path"] for row in finding)]
        paths.extend(row[0] for row in db.execute("SELECT image_path FROM frames WHERE video_id=?", (video_id,)))
        root = asset_dir.resolve()
        files, frame_dir = _owned_files(root, video_id, paths, original)
        db.execute("""DELETE FROM task_events WHERE task_type='processing'
                      AND task_id IN (SELECT id FROM processing_tasks WHERE video_id=?)""", (video_id,))
        db.execute("DELETE FROM processing_tasks WHERE video_id=?", (video_id,))
        for row in finding:
            db.execute("DELETE FROM task_events WHERE task_type='finding' AND task_id=?", (row["id"],))
            db.execute("DELETE FROM finding_tasks WHERE id=?", (row["id"],))
            parent = row["parent_id"]
            if parent and db.execute("""
                DELETE FROM finding_tasks WHERE id=? AND status='split'
                  AND NOT EXISTS (SELECT 1 FROM finding_tasks WHERE parent_id=?)
            """, (parent, parent)).rowcount:
                db.execute("DELETE FROM task_events WHERE task_type='finding' AND task_id=?", (parent,))
        # Preserve token and runtime totals while detaching the removed source.
        db.execute("UPDATE ai_usage SET video_id=NULL WHERE video_id=?", (video_id,))
        db.execute("UPDATE local_visual_usage SET video_id=NULL,error=NULL WHERE video_id=?", (video_id,))
        db.execute("DELETE FROM videos WHERE id=?", (video_id,))
        retained = {Path(row[0]).resolve() for row in db.execute("""
            SELECT path FROM uploaded_sources WHERE path<>''
            UNION SELECT source_photo FROM videos WHERE source_photo IS NOT NULL
            UNION SELECT photo_path FROM finding_tasks WHERE photo_path IS NOT NULL
            UNION SELECT image_path FROM frames
        """)}
        shared_original = Path(original).resolve() if original else None
        if shared_original in retained:
            files = {path for path in files if not (
                path.parent == shared_original.parent and path.name.startswith(shared_original.stem + "-"))}
        files -= retained
        from . import visual
        for path in files:
            if path.suffix.lower() in {".png", ".jpg", ".jpeg"} and path.is_file():
                visual.discard_image_cache(db, path)
    failed = []
    for path in files:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            failed.append(str(path))
    try:
        if frame_dir.parent == (root / "frames").resolve() and frame_dir.is_relative_to(root):
            frame_dir.rmdir()
    except OSError:
        pass  # An absent or non-empty directory requires no further cleanup.
    return failed
