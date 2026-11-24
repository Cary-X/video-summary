"""Windows drop window for the shared Raccoon Agent inbox."""
import configparser
from contextlib import closing
from pathlib import Path
import re
import sqlite3
import tkinter as tk
from tkinter import filedialog, ttk
import webbrowser

from PIL import Image, ImageTk
from tkinterdnd2 import DND_FILES, DND_TEXT, TkinterDnD

from pva import desktop_inbox, store

ASSETS = Path(__file__).resolve().parent / "assets"
ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
METHODS = {"Automatic": "auto", "Quick": "direct", "Detailed": "batched"}
TYPES = {"Detect automatically": "auto", "Video": "video", "Webpage": "webpage"}
PAPER, TAN, INK = "#fff8ec", "#eacaa7", "#302319"
PANEL, EDGE = "#f8ebd8", "#8b6243"


def main():
    window = TkinterDnD.Tk()
    window.title("Raccoon Box")
    window.iconbitmap(str(ASSETS / "raccoon-box.ico"))
    window.attributes("-topmost", True)
    window.configure(bg=PAPER, padx=16, pady=14)
    window.minsize(365, 300)
    with closing(store.connect(DATA / "personal.db")) as db:
        prefs = dict(db.execute("SELECT key,value FROM preferences"))
    automatic = tk.BooleanVar(value=prefs.get("desktop_auto") == "1")
    method = tk.StringVar(value=prefs.get("desktop_method", "Automatic"))
    override = tk.StringVar(value=prefs.get("desktop_source_type", "Detect automatically"))
    status = tk.StringVar()
    summary_count = tk.StringVar()

    def open_browser(library=False):
        webbrowser.open("http://127.0.0.1:8507/" + ("?view=library" if library else ""))

    def update_count():
        if bubble.state() != "withdrawn":
            with closing(store.connect(DATA / "personal.db")) as db:
                count = db.execute("SELECT COUNT(*) FROM videos WHERE status='ready' AND summary<>''").fetchone()[0]
            summary_count.set(f"{count} summaries are ready in your Library.")

    def poll_count():
        update_count()
        window.after(2500, poll_count)

    def acknowledge():
        bubble.withdraw()
        status.set("")

    def save_settings(*_):
        with closing(store.connect(DATA / "personal.db")) as db, db:
            db.executemany("""INSERT INTO preferences(key,value) VALUES (?,?)
              ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                [("desktop_auto", str(int(automatic.get()))), ("desktop_method", method.get()),
                 ("desktop_source_type", override.get())])

    def show_confirmation():
        bubble.update_idletasks()
        width, height = bubble.winfo_reqwidth(), bubble.winfo_reqheight()
        x = window.winfo_rootx() + window.winfo_width() + 10
        if x + width > window.winfo_screenwidth():
            x = max(0, window.winfo_rootx() - width - 10)
        y = min(queue_label.winfo_rooty(), window.winfo_screenheight() - height)
        bubble.geometry(f"+{x}+{max(0, y)}")
        bubble.deiconify()
        bubble.lift()

    def accept(sources):
        added, errors = 0, []
        with closing(store.connect(DATA / "personal.db")) as db:
            for source in dict.fromkeys(sources):
                try:
                    if Path(source).suffix.lower() == ".url" and Path(source).is_file():
                        shortcut = configparser.ConfigParser(interpolation=None)
                        shortcut.read(source, encoding="utf-8-sig")
                        source = shortcut.get("InternetShortcut", "URL")
                    desktop_inbox.add(db, DATA, source, automatic.get(), METHODS[method.get()], TYPES[override.get()])
                    added += 1
                except (OSError, ValueError, configparser.Error) as exc:
                    errors.append(str(exc))
        status.set(errors[0] if errors else "")
        if added:
            received.set("Got it!" if added == 1 else f"Got it! {added} items received.")
            show_confirmation()
            update_count()
            update_box_status()

    def drop(event):
        data = event.data.strip().strip("\x00")
        try:
            files = window.tk.splitlist(data)
        except tk.TclError:
            files = ()
        if files and all(Path(item).is_file() for item in files):
            accept(files)
        else:
            accept(re.findall(r'https?://[^\s<>{}"]+', data) or [data])
        return event.action

    busy = False
    zone = tk.Canvas(window, height=252, background=PAPER, highlightthickness=0)
    zone.pack(fill="x", pady=(2, 4))
    queue_status = tk.StringVar(value="0 processing · 0 queued")
    queue_label = tk.Label(window, textvariable=queue_status, bg=PAPER, fg=INK,
                           font=("Trebuchet MS", 11, "bold"))
    queue_label.pack(fill="x", pady=(0, 10))

    with Image.open(ASSETS / "r-library-box-v2.png") as source:
        box_art = source.convert("RGBA")
    box_art = box_art.crop(box_art.getchannel("A").getbbox())
    box_photo = None
    box_width = 0
    activity_text = None

    def draw_zone(event=None):
        nonlocal box_photo, box_width, activity_text
        width = event.width if event else zone.winfo_width()
        if width <= 28:
            return
        if width != box_width:
            illustration = box_art.copy()
            illustration.thumbnail((width - 28, 194), Image.Resampling.LANCZOS)
            box_photo = ImageTk.PhotoImage(illustration, master=zone)
            box_width = width
            zone.delete("all")
            zone.create_rectangle(4, 4, width - 2, 250, fill=TAN, outline="")
            zone.create_rectangle(1, 1, width - 5, 247, fill=PANEL,
                                  outline=EDGE, width=2)
            zone.create_image(width / 2, 103, image=box_photo)
            activity_text = zone.create_text(width / 2, 223, fill=INK,
                                             font=("Trebuchet MS", 12, "bold"))
        zone.itemconfigure(activity_text, text="Working on your queue…" if busy else "Drop a link or file")

    def refresh_box():
        nonlocal busy
        with closing(store.connect(DATA / "personal.db")) as db:
            running, queued = db.execute("""
                SELECT COUNT(CASE WHEN status = 'running' THEN 1 END),
                       COUNT(CASE WHEN status = 'queued' THEN 1 END)
                FROM (
                    SELECT status FROM processing_tasks WHERE status IN ('running', 'queued')
                    UNION ALL
                    SELECT status FROM finding_tasks WHERE status IN ('running', 'queued')
                    UNION ALL
                    SELECT 'queued' FROM desktop_inbox
                    WHERE automatic=1 AND status IN ('pending', 'sending')
                )
            """).fetchone()
        busy = bool(running or queued)
        queue_status.set(f"{running} processing · {queued} queued")
        draw_zone()

    def update_box_status():
        try:
            refresh_box()
        except sqlite3.OperationalError:
            queue_status.set("Refreshing queue…")

    def update_box():
        update_box_status()
        window.after(1500, update_box)

    zone.bind("<Configure>", draw_zone)
    for target in (window, zone):
        target.drop_target_register(DND_FILES, DND_TEXT, "DND_URL")
        target.dnd_bind("<<Drop>>", drop)

    bubble = tk.Toplevel(window, bg=PAPER)
    bubble.withdraw()
    bubble.overrideredirect(True)
    bubble.transient(window)
    bubble.attributes("-topmost", True)
    window.bind("<Unmap>", lambda event: bubble.withdraw() if event.widget is window else None)
    received = tk.StringVar()
    speech = tk.Frame(bubble, bg=TAN, highlightbackground=INK, highlightthickness=2, padx=10, pady=7)
    speech.pack(side="left", fill="both", expand=True)
    tk.Label(speech, textvariable=received, bg=TAN, fg=INK,
             font=("Segoe UI", 12, "bold")).pack(anchor="w")
    tk.Label(speech, textvariable=summary_count, bg=TAN, fg=INK, wraplength=240,
             justify="left", font=("Segoe UI", 10)).pack(anchor="w", pady=(3, 6))
    actions = tk.Frame(speech, bg=TAN)
    actions.pack(anchor="w")
    ttk.Button(actions, text="See summaries", command=lambda: open_browser(library=True)).pack(side="left")
    ttk.Button(actions, text="Got it", command=acknowledge).pack(side="left", padx=(6, 0))
    window.after(2500, poll_count)
    settings = tk.Frame(window, bg=PAPER, padx=8, pady=7)
    settings_label = tk.StringVar(value="▶  Settings")
    def toggle_settings():
        if settings.winfo_manager():
            settings.pack_forget()
            settings_label.set("▶  Settings")
        else:
            settings.pack(fill="x", after=settings_button)
            settings_label.set("▼  Settings")

    settings_button = tk.Button(window, textvariable=settings_label, command=toggle_settings,
                                bg=TAN, fg=INK, activebackground="#eacaa7",
                                font=("Segoe UI", 11, "bold"), anchor="w", padx=10)
    settings_button.pack(fill="x")
    ttk.Checkbutton(settings, text="Process automatically", variable=automatic).pack(anchor="w")
    ttk.Label(settings, text="Summary method").pack(anchor="w", pady=(8, 0))
    ttk.Combobox(settings, textvariable=method, values=list(METHODS), state="readonly").pack(fill="x")
    ttk.Label(settings, text="Link type").pack(anchor="w", pady=(8, 0))
    ttk.Combobox(settings, textvariable=override, values=list(TYPES), state="readonly").pack(fill="x")
    for setting in (automatic, method, override):
        setting.trace_add("write", save_settings)
    window.after(100, update_box)
    buttons = ttk.Frame(settings)
    buttons.pack(fill="x", pady=10)
    ttk.Button(buttons, text="Choose files", command=lambda: accept(filedialog.askopenfilenames())).pack(side="left")
    def paste():
        try:
            accept(re.findall(r'https?://[^\s<>{}"]+', window.clipboard_get()))
        except tk.TclError:
            status.set("Copy a link first.")

    ttk.Button(buttons, text="Paste link", command=paste).pack(side="left")
    ttk.Button(buttons, text="Open Library", command=lambda: open_browser(library=True)).pack(side="right")
    ttk.Label(window, textvariable=status, wraplength=300).pack(anchor="w")
    window.mainloop()


if __name__ == "__main__":
    main()
