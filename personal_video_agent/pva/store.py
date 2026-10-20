"""SQLite persistence and a small, inspectable processing audit trail."""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


_schema_lock = threading.Lock()
_initialized_paths: set[str] = set()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=10000")
    resolved = str(path.resolve())
    with _schema_lock:
        if resolved in _initialized_paths:
            return db
        if db.execute("PRAGMA journal_mode").fetchone()[0].casefold() != "wal":
            db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
    CREATE TABLE IF NOT EXISTS videos (
      id TEXT PRIMARY KEY, url TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
      author TEXT NOT NULL DEFAULT '', published_at TEXT NOT NULL DEFAULT '',
      duration REAL, source_photo TEXT, status TEXT NOT NULL DEFAULT 'new',
      stage TEXT NOT NULL DEFAULT 'new', error TEXT, language TEXT,
      target_language TEXT NOT NULL DEFAULT 'en', transcript_source TEXT,
      summary TEXT, category TEXT, created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS segments (
      id INTEGER PRIMARY KEY, video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
      seq INTEGER NOT NULL, start REAL NOT NULL, end REAL NOT NULL,
      text TEXT NOT NULL, translated_text TEXT, UNIQUE(video_id, seq)
    );
    CREATE TABLE IF NOT EXISTS frames (
      id INTEGER PRIMARY KEY, video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
      segment_id INTEGER REFERENCES segments(id), at_seconds REAL NOT NULL,
      image_path TEXT NOT NULL, extracted_text TEXT NOT NULL DEFAULT '',
      UNIQUE(video_id, segment_id)
    );
    CREATE TABLE IF NOT EXISTS claims (
      id INTEGER PRIMARY KEY, video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
      ordinal INTEGER NOT NULL, text TEXT NOT NULL, evidence_json TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS events (
      id INTEGER PRIMARY KEY, video_id TEXT REFERENCES videos(id) ON DELETE CASCADE,
      at TEXT NOT NULL, stage TEXT NOT NULL, detail TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS feedback (
      id INTEGER PRIMARY KEY, video_id TEXT REFERENCES videos(id) ON DELETE CASCADE,
      at TEXT NOT NULL, rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
      note TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS preferences (
      key TEXT PRIMARY KEY, value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS finding_tasks (
      id TEXT PRIMARY KEY, kind TEXT NOT NULL, source TEXT NOT NULL,
      photo_path TEXT, parent_id TEXT, card_json TEXT,
      candidates_json TEXT NOT NULL DEFAULT '[]',
      status TEXT NOT NULL DEFAULT 'queued', stage TEXT NOT NULL DEFAULT 'queued',
      progress REAL NOT NULL DEFAULT 0, error TEXT,
      video_id TEXT REFERENCES videos(id),
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS processing_tasks (
      id TEXT PRIMARY KEY, video_id TEXT NOT NULL REFERENCES videos(id),
      finding_task_id TEXT REFERENCES finding_tasks(id),
      mode TEXT NOT NULL DEFAULT 'ingest',
      status TEXT NOT NULL DEFAULT 'queued', stage TEXT NOT NULL DEFAULT 'queued',
      progress REAL NOT NULL DEFAULT 0, error TEXT,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS task_events (
      id INTEGER PRIMARY KEY, task_type TEXT NOT NULL, task_id TEXT NOT NULL,
      at TEXT NOT NULL, stage TEXT NOT NULL, detail TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS ai_usage (
      id INTEGER PRIMARY KEY, at TEXT NOT NULL, video_id TEXT REFERENCES videos(id),
      finding_task_id TEXT, purpose TEXT NOT NULL,
      requested_model TEXT NOT NULL, response_model TEXT NOT NULL,
      prompt_tokens INTEGER NOT NULL, completion_tokens INTEGER NOT NULL,
      total_tokens INTEGER NOT NULL, cache_hit_tokens INTEGER NOT NULL,
      cache_miss_tokens INTEGER NOT NULL,
      estimated_cost_low_usd REAL, estimated_cost_high_usd REAL
    );
    CREATE INDEX IF NOT EXISTS idx_segments_video ON segments(video_id, seq);
    CREATE INDEX IF NOT EXISTS idx_videos_category ON videos(category, created_at);
    CREATE INDEX IF NOT EXISTS idx_finding_status ON finding_tasks(status, created_at);
    CREATE INDEX IF NOT EXISTS idx_processing_status ON processing_tasks(status, created_at);
    CREATE INDEX IF NOT EXISTS idx_ai_usage_at ON ai_usage(at);
    CREATE INDEX IF NOT EXISTS idx_ai_usage_video ON ai_usage(video_id);
        """)
        if "mode" not in {row["name"] for row in db.execute("PRAGMA table_info(processing_tasks)")}:
            db.execute("ALTER TABLE processing_tasks ADD COLUMN mode TEXT NOT NULL DEFAULT 'ingest'")
        _initialized_paths.add(resolved)
    return db


def record_ai_usage(db: sqlite3.Connection, *, video_id: str | None,
                    finding_task_id: str | None, purpose: str,
                    requested_model: str, response_model: str, usage: dict) -> None:
    """Save provider-reported tokens; dollar figures are a price range, not a bill."""
    prompt = max(0, int(usage.get("prompt_tokens") or 0))
    completion = max(0, int(usage.get("completion_tokens") or 0))
    total = max(0, int(usage.get("total_tokens") or prompt + completion))
    hit = max(0, int(usage.get("prompt_cache_hit_tokens") or
                     (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0))
    miss = max(0, int(usage.get("prompt_cache_miss_tokens") or max(0, prompt - hit)))
    low = high = None
    if requested_model == "deepseek-flash":
        # DeepSeek public off-peak/peak USD prices per million tokens, 2026-10-20.
        low = (hit * 0.003 + miss * 0.15 + completion * 0.60) / 1_000_000
        high = low * 2
    with db:
        db.execute("""INSERT INTO ai_usage
          (at,video_id,finding_task_id,purpose,requested_model,response_model,
           prompt_tokens,completion_tokens,total_tokens,cache_hit_tokens,
           cache_miss_tokens,estimated_cost_low_usd,estimated_cost_high_usd)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (now(), video_id, finding_task_id, purpose, requested_model,
           response_model, prompt, completion, total, hit, miss, low, high))


def add_video(db: sqlite3.Connection, url: str, *, title: str, author: str = "",
              published_at: str = "", duration: float | None = None,
              photo: str | None = None, target_language: str = "en") -> str:
    existing = db.execute("SELECT id FROM videos WHERE url=?", (url,)).fetchone()
    if existing:
        return existing["id"]
    video_id = uuid.uuid4().hex
    timestamp = now()
    with db:
        db.execute("""INSERT INTO videos
          (id,url,title,author,published_at,duration,source_photo,target_language,created_at,updated_at)
          VALUES (?,?,?,?,?,?,?,?,?,?)""",
          (video_id,url,title,author,published_at,duration,photo,target_language,timestamp,timestamp))
        event(db, video_id, "new", "Video added")
    return video_id


def event(db: sqlite3.Connection, video_id: str, stage: str, detail: str) -> None:
    db.execute("INSERT INTO events(video_id,at,stage,detail) VALUES (?,?,?,?)",
               (video_id, now(), stage, detail))
    db.execute("UPDATE videos SET stage=?,updated_at=? WHERE id=?", (stage, now(), video_id))


def replace_segments(db: sqlite3.Connection, video_id: str, segments: list[dict]) -> None:
    with db:
        db.execute("DELETE FROM frames WHERE video_id=?", (video_id,))
        db.execute("DELETE FROM claims WHERE video_id=?", (video_id,))
        db.execute("DELETE FROM segments WHERE video_id=?", (video_id,))
        db.executemany("""INSERT INTO segments(video_id,seq,start,end,text)
          VALUES (?,?,?,?,?)""", [(video_id, i, s["start"], s["end"], s["text"])
                             for i, s in enumerate(segments)])
        event(db, video_id, "transcribed", f"Stored {len(segments)} timestamped segments")


def save_result(db: sqlite3.Connection, video_id: str, summary: str, category: str,
                claims: list[dict]) -> None:
    with db:
        db.execute("DELETE FROM claims WHERE video_id=?", (video_id,))
        db.executemany("""INSERT INTO claims(video_id,ordinal,text,evidence_json)
          VALUES (?,?,?,?)""", [(video_id, i, c["text"], json.dumps(c["evidence"]))
                             for i, c in enumerate(claims)])
        db.execute("""UPDATE videos SET summary=?,category=?,status='ready',error=NULL,
          updated_at=? WHERE id=?""", (summary, category, now(), video_id))
        event(db, video_id, "ready", f"Summary saved with {len(claims)} traced claims")


def detail(db: sqlite3.Connection, video_id: str) -> dict:
    row = db.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
    if row is None:
        raise KeyError(video_id)
    result = dict(row)
    for name in ("segments", "frames", "claims", "events", "feedback"):
        order = "seq" if name == "segments" else "ordinal" if name == "claims" else "id"
        result[name] = [dict(x) for x in db.execute(
            f"SELECT * FROM {name} WHERE video_id=? ORDER BY {order}", (video_id,))]
    for claim in result["claims"]:
        claim["evidence"] = json.loads(claim.pop("evidence_json"))
    return result
