"""Persistent finding and processing queues for the local Streamlit app."""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import intelligence, media, pipeline, store


def _task_event(db: sqlite3.Connection, kind: str, task_id: str,
                stage: str, detail: str) -> None:
    db.execute("INSERT INTO task_events(task_type,task_id,at,stage,detail) VALUES (?,?,?,?,?)",
               (kind, task_id, store.now(), stage, detail[:1500]))


def _update(db: sqlite3.Connection, table: str, task_id: str, *, status: str,
            stage: str, progress: float, detail: str = "", error: str | None = None) -> None:
    kind = "finding" if table == "finding_tasks" else "processing"
    with db:
        db.execute(f"""UPDATE {table} SET status=?,stage=?,progress=?,error=?,updated_at=?
                   WHERE id=?""", (status, stage, max(0, min(1, progress)), error,
                                    store.now(), task_id))
        _task_event(db, kind, task_id, stage, detail or stage)


def enqueue_find(db: sqlite3.Connection, kind: str, source: str,
                 *, photo_path: str | None = None, card: dict | None = None,
                 parent_id: str | None = None) -> str:
    if kind not in {"photo", "card", "link"}:
        raise ValueError("Unsupported finding task")
    task_id = uuid.uuid4().hex
    with db:
        db.execute("""INSERT INTO finding_tasks
          (id,kind,source,photo_path,parent_id,card_json,created_at,updated_at)
          VALUES (?,?,?,?,?,?,?,?)""",
          (task_id, kind, source, photo_path, parent_id,
           json.dumps(card, ensure_ascii=False) if card else None, store.now(), store.now()))
        _task_event(db, "finding", task_id, "queued", "Waiting to find the video")
    return task_id


def enqueue_processing(db: sqlite3.Connection, finding_task_id: str,
                       candidate_index: int, target_language: str) -> str:
    row = db.execute("SELECT * FROM finding_tasks WHERE id=?", (finding_task_id,)).fetchone()
    if row is None or row["status"] != "ready":
        raise ValueError("The finding task is not ready to process")
    candidates = json.loads(row["candidates_json"])
    if not 0 <= candidate_index < len(candidates):
        raise ValueError("Choose a search result")
    chosen = candidates[candidate_index]
    card = json.loads(row["card_json"] or "{}")
    video_id = store.add_video(
        db, chosen["url"], title=chosen.get("title") or card.get("title") or "Untitled",
        author=chosen.get("author") or card.get("author") or "",
        published_at=chosen.get("published_at") or "",
        photo=row["photo_path"], target_language=target_language)
    task_id = uuid.uuid4().hex
    with db:
        db.execute("""INSERT INTO processing_tasks
          (id,video_id,finding_task_id,created_at,updated_at)
          VALUES (?,?,?,?,?)""", (task_id, video_id, finding_task_id, store.now(), store.now()))
        db.execute("""UPDATE finding_tasks SET status='delivered',stage='delivered',
          progress=1,video_id=?,updated_at=? WHERE id=?""",
          (video_id, store.now(), finding_task_id))
        _task_event(db, "finding", finding_task_id, "delivered", "Moved to Processing")
        _task_event(db, "processing", task_id, "queued", "Waiting to process the video")
    return task_id


def enqueue_processing_video(db: sqlite3.Connection, video_id: str,
                             *, mode: str = "ingest") -> str:
    if db.execute("SELECT 1 FROM videos WHERE id=?", (video_id,)).fetchone() is None:
        raise KeyError(video_id)
    if mode not in {"ingest", "repair_visual"}:
        raise ValueError("Unsupported processing mode")
    task_id = uuid.uuid4().hex
    with db:
        db.execute("""INSERT INTO processing_tasks
          (id,video_id,mode,created_at,updated_at) VALUES (?,?,?,?,?)""",
          (task_id, video_id, mode, store.now(), store.now()))
        _task_event(db, "processing", task_id, "queued", "Waiting to process the video")
    return task_id


def cookie_browser(db: sqlite3.Connection) -> str:
    row = db.execute("SELECT value FROM preferences WHERE key='youtube_cookie_browser'").fetchone()
    return row["value"] if row else ""


class TaskManager:
    def __init__(self, db_path: Path, asset_dir: Path, skill_file: Path):
        self.db_path = db_path
        self.asset_dir = asset_dir
        self.skill_file = skill_file
        self.find_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pva-find")
        self.process_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pva-process")
        self.lock = threading.Lock()
        self.find_active: set[str] = set()
        self.process_active: set[str] = set()
        db = store.connect(db_path)
        with db:
            db.execute("UPDATE finding_tasks SET status='queued',stage='resuming' WHERE status='running'")
            db.execute("UPDATE processing_tasks SET status='queued',stage='resuming' WHERE status='running'")
        db.close()

    def pump(self) -> None:
        """Claim only available slots; a failed task cannot stop the next one."""
        with self.lock:
            db = store.connect(self.db_path)
            try:
                for table, active, pool, run in (
                    ("finding_tasks", self.find_active, self.find_pool, self._find),
                    ("processing_tasks", self.process_active, self.process_pool, self._process),
                ):
                    capacity = 2 - len(active)
                    if capacity <= 0:
                        continue
                    rows = db.execute(
                        f"SELECT id FROM {table} WHERE status='queued' ORDER BY created_at,id LIMIT ?",
                        (capacity,)).fetchall()
                    for row in rows:
                        task_id = row["id"]
                        with db:
                            claimed = db.execute(
                                f"UPDATE {table} SET status='running',stage='starting',updated_at=? "
                                "WHERE id=? AND status='queued'", (store.now(), task_id)).rowcount
                        if claimed:
                            active.add(task_id)
                            pool.submit(run, task_id)
            finally:
                db.close()

    def _done(self, task_id: str, *, finding: bool) -> None:
        with self.lock:
            (self.find_active if finding else self.process_active).discard(task_id)
        self.pump()

    def _find(self, task_id: str) -> None:
        db = store.connect(self.db_path)
        try:
            row = db.execute("SELECT * FROM finding_tasks WHERE id=?", (task_id,)).fetchone()
            kind = row["kind"]
            if kind == "photo":
                _update(db, "finding_tasks", task_id, status="running", stage="identifying",
                        progress=0.15, detail="DeepSeek is finding distinct video cards")
                with intelligence.usage_context(self.db_path, finding_task_id=task_id):
                    identified = intelligence.identify_video_photo(Path(row["photo_path"]))
                for card in identified["videos"]:
                    enqueue_find(db, "card", row["photo_path"],
                                 photo_path=row["photo_path"], card=card, parent_id=task_id)
                _update(db, "finding_tasks", task_id, status="split", stage="identified",
                        progress=1, detail=f"Created {len(identified['videos'])} separate video tasks")
            elif kind == "link":
                _update(db, "finding_tasks", task_id, status="running", stage="metadata",
                        progress=0.3, detail="Resolving link metadata")
                source = media.metadata(row["source"], cookie_browser=cookie_browser(db))
                candidate = {key: source[key] for key in
                             ("url", "title", "author", "published_at", "duration")}
                candidate.update({"score": 1.0, "verified": True,
                                  "confidence": 1.0, "reason": "Direct link supplied by user"})
                with db:
                    db.execute("UPDATE finding_tasks SET candidates_json=? WHERE id=?",
                               (json.dumps([candidate], ensure_ascii=False), task_id))
                _update(db, "finding_tasks", task_id, status="ready", stage="found",
                        progress=1, detail="Link resolved; ready to process")
            else:
                card = json.loads(row["card_json"] or "{}")
                _update(db, "finding_tasks", task_id, status="running", stage="searching",
                        progress=0.20, detail=f"Searching YouTube for {card.get('title', 'video')}")
                candidates = media.search_candidates(
                    card.get("title", ""), card.get("author", ""),
                    card.get("published_at", ""), limit=8,
                    cookie_browser=cookie_browser(db))
                if not candidates:
                    raise ValueError("No video search results found")
                _update(db, "finding_tasks", task_id, status="running", stage="checking",
                        progress=0.65, detail="DeepSeek is checking the search matches")
                with intelligence.usage_context(self.db_path, finding_task_id=task_id):
                    checked = intelligence.verify_video_matches(card, candidates)
                with db:
                    db.execute("UPDATE finding_tasks SET candidates_json=? WHERE id=?",
                               (json.dumps(checked, ensure_ascii=False), task_id))
                _update(db, "finding_tasks", task_id, status="ready", stage="found",
                        progress=1, detail="Search results checked; choose a match")
        except Exception as exc:
            _update(db, "finding_tasks", task_id, status="failed", stage="failed",
                    progress=1, detail=str(exc), error=str(exc))
        finally:
            db.close()
            self._done(task_id, finding=True)

    def _process(self, task_id: str) -> None:
        db = store.connect(self.db_path)
        try:
            row = db.execute("SELECT * FROM processing_tasks WHERE id=?", (task_id,)).fetchone()

            def report(stage: str, fraction: float, detail: str) -> None:
                _update(db, "processing_tasks", task_id, status="running", stage=stage,
                        progress=fraction, detail=detail)

            with intelligence.usage_context(self.db_path, video_id=row["video_id"]):
                if row["mode"] == "repair_visual":
                    pipeline.repair_visuals(db, row["video_id"], asset_dir=self.asset_dir,
                                            skill_file=self.skill_file,
                                            cookie_browser=cookie_browser(db),
                                            on_progress=report)
                else:
                    pipeline.process(db, row["video_id"], asset_dir=self.asset_dir,
                                     skill_file=self.skill_file, cookie_browser=cookie_browser(db),
                                     on_progress=report)
            _update(db, "processing_tasks", task_id, status="ready", stage="complete",
                    progress=1, detail="Video summary is ready in Library")
        except Exception as exc:
            _update(db, "processing_tasks", task_id, status="failed", stage="failed",
                    progress=1, detail=str(exc), error=str(exc))
        finally:
            db.close()
            self._done(task_id, finding=False)


_manager: TaskManager | None = None
_manager_lock = threading.Lock()


def get_manager(db_path: Path, asset_dir: Path, skill_file: Path) -> TaskManager:
    global _manager
    with _manager_lock:
        if _manager is None or _manager.db_path != db_path:
            _manager = TaskManager(db_path, asset_dir, skill_file)
    _manager.pump()
    return _manager
