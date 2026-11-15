"""Use the files bundled beside the program and relocate saved file references."""
from pathlib import Path
import os


def configure(project, db):
    data = project / "data"
    os.environ["PATH"] = str(project / "runtime" / "media") + os.pathsep + os.environ.get("PATH", "")
    os.environ["HF_HOME"] = str(data / "models" / "huggingface")
    saved = db.execute("SELECT value FROM preferences WHERE key='installation_root'").fetchone()
    previous = Path(saved[0]) if saved else project
    if previous == project:
        return
    fields = {
        "videos": ("source_photo",),
        "frames": ("image_path",),
        "finding_tasks": ("source", "photo_path"),
        "uploaded_sources": ("path",),
        "desktop_inbox": ("source",),
    }
    with db:
        for table, columns in fields.items():
            for column in columns:
                for row_id, raw in db.execute(f"SELECT rowid,{column} FROM {table} WHERE {column} IS NOT NULL").fetchall():
                    path = Path(raw)
                    if path.is_absolute() and path.is_relative_to(previous / "data"):
                        new_path = data / path.relative_to(previous / "data")
                        db.execute(f"UPDATE {table} SET {column}=? WHERE rowid=?", (str(new_path), row_id))
        db.execute("""INSERT INTO preferences(key,value) VALUES('installation_root',?)
                      ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (str(project),))
