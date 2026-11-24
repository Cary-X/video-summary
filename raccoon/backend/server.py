"""Run the web server and desktop inbox consumer in one process."""
from contextlib import closing
import logging
from pathlib import Path
import sys
import threading

from streamlit.web import cli

from pva import desktop_inbox, intelligence, store, tasks, usage_visuals
from backend.installation import configure

PROJECT = Path(__file__).resolve().parents[1]
DATA = PROJECT / "data"
DB = DATA / "personal.db"


def consume():
    interval = threading.Event()
    while True:
        try:
            with closing(store.connect(DB)) as db:
                items = db.execute("""SELECT * FROM desktop_inbox
                    WHERE automatic=1 AND status='pending' ORDER BY id LIMIT 3""").fetchall()
                for item in items:
                    desktop_inbox.submit(db, DATA, item)
            tasks.get_manager(DB, DATA, PROJECT / "backend" / "summary-skill" / "SKILL.md")
        except Exception:
            logging.exception("Desktop inbox worker could not complete its poll")
        interval.wait(1)


def main():
    with closing(store.connect(DB)) as db, db:
        configure(PROJECT, db)
        db.execute("""UPDATE desktop_inbox SET status='failed',
                      error='Previous submission was interrupted. Check Processing before retrying.'
                      WHERE status='sending'""")
    intelligence._load_env()
    usage_visuals.automatic_timezone()
    usage_visuals.timezone_options()
    threading.Thread(target=consume, name="desktop-inbox", daemon=True).start()
    sys.argv = ["streamlit", "run", str(PROJECT / "frontend" / "app.py"), "--server.address", "127.0.0.1",
                "--server.port", "8507", "--server.headless", "true", *sys.argv[1:]]
    cli.main()


if __name__ == "__main__":
    main()
