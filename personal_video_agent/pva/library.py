"""Library filters and personal collections."""
import sqlite3
from datetime import datetime
from urllib.parse import urlsplit

from . import presentation

SOURCES = {"video": "Video", "audio": "Audio", "document": "File", "webpage": "Web URL"}
VIDEO_PLATFORMS = {
    "YouTube": ("youtube.com", "youtu.be", "youtube-nocookie.com"),
    "Bilibili": ("bilibili.com", "bilibili.tv", "b23.tv"),
    "TikTok": ("tiktok.com",),
    "Douyin": ("douyin.com", "iesdouyin.com"),
    "Vimeo": ("vimeo.com",),
    "Twitch": ("twitch.tv",),
    "Dailymotion": ("dailymotion.com", "dai.ly"),
    "Facebook": ("facebook.com", "fb.watch"),
    "Instagram": ("instagram.com",),
    "X": ("x.com", "twitter.com"),
}


def source_label(entry):
    kind = entry["source_kind"]
    if kind != "video":
        return SOURCES[kind]
    try:
        host = (urlsplit(entry.get("url") or "").hostname or "").rstrip(".").lower()
    except ValueError:
        return SOURCES["video"]
    for name, domains in VIDEO_PLATFORMS.items():
        if any(host == domain or host.endswith("." + domain) for domain in domains):
            return f"Video · {name}"
    return f"Video · {host.removeprefix('www.')}" if host else "Video"


def entries(db):
    return [dict(row) for row in db.execute("""
        SELECT v.id,v.url,v.title,v.author,v.category,v.published_at,v.created_at,v.status,
               COALESCE(u.kind,'video') AS source_kind
        FROM videos v LEFT JOIN uploaded_sources u ON u.video_id=v.id
        WHERE v.status!='cancelled'
    """)]


def source_date(value):
    try:
        return datetime.fromisoformat(str(value).strip().replace("Z", "+00:00")).date()
    except ValueError:
        return None


def filtered(rows, memberships, *, collections, topics, sources, dates, field, order):
    result = []
    for item in rows:
        if collections and not collections.intersection(memberships.get(item["id"], ())):
            continue
        if topics and (item["category"] or "") not in topics:
            continue
        if sources and item["source_kind"] not in sources:
            continue
        if len(dates) == 2:
            day = source_date(item[field])
            if day is None or not dates[0] <= day <= dates[1]:
                continue
        result.append(item)
    if order == "Title A–Z":
        return sorted(result, key=lambda row: (row["title"].casefold(), row["id"]))
    return presentation.order_library(result, date_field=field, newest_first=order == "Latest first")


def create_collection(db, name):
    name = " ".join(name.split())
    if not 1 <= len(name) <= 60:
        raise ValueError("Enter a collection name of 1 to 60 characters.")
    try:
        with db:
            cursor = db.execute("INSERT INTO collections(name,name_key) VALUES(?,?)", (name, name.casefold()))
    except sqlite3.IntegrityError:
        raise ValueError("A collection with this name already exists.") from None
    return cursor.lastrowid


def collection_state(db):
    names = {row["id"]: row["name"] for row in db.execute("SELECT id,name FROM collections ORDER BY name COLLATE NOCASE")}
    memberships = {}
    for row in db.execute("SELECT collection_id,video_id FROM collection_items"):
        memberships.setdefault(row["video_id"], set()).add(row["collection_id"])
    return names, memberships


def save_collections(db, video_id, collection_ids, new_name=""):
    # Create the collection and attach it in the same transaction.
    ids = set(collection_ids)
    with db:
        if new_name.strip():
            name = " ".join(new_name.split())
            if len(name) > 60:
                raise ValueError("Use a collection name of up to 60 characters.")
            db.execute("INSERT INTO collections(name,name_key) VALUES(?,?) ON CONFLICT(name_key) DO NOTHING",
                       (name, name.casefold()))
            ids.add(db.execute("SELECT id FROM collections WHERE name_key=?", (name.casefold(),)).fetchone()[0])
        db.execute("DELETE FROM collection_items WHERE video_id=?", (video_id,))
        db.executemany("INSERT INTO collection_items(collection_id,video_id) VALUES(?,?)",
                       [(collection_id, video_id) for collection_id in ids])


def suggested_collections(db, entry):
    """Rank collections by the user's existing saves with this topic or source kind."""
    return [row["collection_id"] for row in db.execute("""
        SELECT c.collection_id,
               SUM(CASE WHEN v.category=? THEN 2 ELSE 0 END
                   + CASE WHEN COALESCE(u.kind,'video')=? THEN 1 ELSE 0 END) AS score
        FROM collection_items c JOIN videos v ON v.id=c.video_id
        LEFT JOIN uploaded_sources u ON u.video_id=v.id
        WHERE v.id<>? AND v.status='ready'
        GROUP BY c.collection_id HAVING score>0 ORDER BY score DESC,c.collection_id
    """, (entry.get("category"), entry["source_kind"], entry["id"]))]
