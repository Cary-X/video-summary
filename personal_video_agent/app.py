"""Local Streamlit interface. Run: python -m streamlit run app.py --server.headless true"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import streamlit as st
from PIL import Image

from pva import intelligence, media, pipeline, presentation, store, tasks, usage_visuals


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DB = DATA / "personal.db"
SKILL = ROOT / "summary-skill" / "SKILL.md"


def photo_region(path: str, bbox: list | None):
    if not bbox:
        return path
    image = Image.open(path)
    width, height = image.size
    left, top, right, bottom = bbox
    return image.crop((int(left * width / 1000), int(top * height / 1000),
                       int(right * width / 1000), int(bottom * height / 1000)))


st.set_page_config(page_title="Personal Video Agent", layout="wide")
st.title("Personal Video Agent")
st.caption("Private video library with separate finding and processing tasks")
db = store.connect(DB)
manager = tasks.get_manager(DB, DATA, SKILL)

with st.sidebar:
    st.header("Preferences")
    languages = {"English": "en", "简体中文": "zh-Hans", "繁體中文": "zh-Hant",
                 "Español": "es", "粵語": "yue"}
    saved_language_row = db.execute(
        "SELECT value FROM preferences WHERE key='summary_language'").fetchone()
    saved_language = saved_language_row["value"] if saved_language_row else "en"
    with st.expander("Summary language (optional)"):
        language_choice = st.selectbox(
            "Write summaries in", list(languages),
            index=next((i for i, code in enumerate(languages.values())
                        if code == saved_language), 0))
    target_language = languages[language_choice]
    if target_language != saved_language:
        with db:
            db.execute("""INSERT INTO preferences(key,value) VALUES('summary_language',?)
              ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (target_language,))
    saved_browser = tasks.cookie_browser(db)
    browsers = ["auto", "firefox", "chrome", "edge", ""]
    cookie_choice = st.selectbox(
        "YouTube login browser", browsers,
        index=browsers.index(saved_browser) if saved_browser in browsers else 0,
        format_func=lambda value: ("Automatic (Firefox, Chrome, Edge)" if value == "auto"
                                   else value.title() if value else "No browser cookies"),
        help="On a YouTube sign-in challenge, try browser cookies. Automatic tries Firefox first. A selected browser is tried first, then the others.")
    if cookie_choice != saved_browser:
        with db:
            db.execute("""INSERT INTO preferences(key,value) VALUES('youtube_cookie_browser',?)
              ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (cookie_choice,))
    sizes = {"Standard": 16, "Large": 20, "Extra large": 24}
    saved_size_row = db.execute(
        "SELECT value FROM preferences WHERE key='reading_size'").fetchone()
    saved_size = saved_size_row["value"] if saved_size_row else "Standard"
    size_choice = st.selectbox(
        "Reading text size", list(sizes),
        index=list(sizes).index(saved_size) if saved_size in sizes else 0)
    if size_choice != saved_size:
        with db:
            db.execute("""INSERT INTO preferences(key,value) VALUES('reading_size',?)
              ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (size_choice,))
    metrics = pipeline.measures(db)
    st.metric("Summarized videos", metrics["ready"])
    st.caption(f"Local database: {DB}")

st.markdown(f"""<style>
.stApp p, .stApp li, .stApp label {{
  font-size: {sizes[size_choice]}px !important;
  line-height: 1.55 !important;
}}
</style>""", unsafe_allow_html=True)

add_tab, find_tab, process_tab, library_tab, measure_tab = st.tabs(
    ["Add video", "Finding", "Processing", "Library", "Measure & learn"])

with add_tab:
    st.write("Paste links, upload photos, or do both. Find sends every input to the Finding area.")
    with st.form("add_inputs", clear_on_submit=True):
        links = st.text_area("Video links (one per line)", height=125,
                             placeholder="https://www.youtube.com/watch?v=...")
        photos = st.file_uploader("Photos or screenshots", type=["jpg", "jpeg", "png"],
                                  accept_multiple_files=True)
        submitted = st.form_submit_button("Find", type="primary")
    if submitted:
        queued, errors = 0, []
        for raw in dict.fromkeys(line.strip() for line in links.splitlines() if line.strip()):
            try:
                tasks.enqueue_find(db, "link", media.valid_url(raw))
                queued += 1
            except Exception as exc:
                errors.append(f"{raw}: {exc}")
        for photo in photos or []:
            try:
                image_dir = DATA / "photos"
                image_dir.mkdir(parents=True, exist_ok=True)
                path = image_dir / (hashlib.sha256(photo.getvalue()).hexdigest() +
                                    Path(photo.name).suffix.lower())
                path.write_bytes(photo.getvalue())
                tasks.enqueue_find(db, "photo", str(path), photo_path=str(path))
                queued += 1
            except Exception as exc:
                errors.append(f"{photo.name}: {exc}")
        manager.pump()
        if queued:
            st.success(f"Sent {queued} input(s) to Finding. The form is clear for more.")
        elif not errors:
            st.info("Paste a link or upload a photo first.")
        for error in errors:
            st.error(error)


@st.fragment(run_every=2)
def finding_panel() -> None:
    manager.pump()
    connection = store.connect(DB)
    try:
        rows = [dict(row) for row in connection.execute(
            """SELECT * FROM finding_tasks WHERE status NOT IN ('delivered','split','skipped')
               ORDER BY created_at DESC,id DESC""")]
        ready = [row for row in rows if row["status"] == "ready"]
        if ready and st.button("Process all AI verified videos", key="process_all"):
            sent = 0
            for row in ready:
                candidates = json.loads(row["candidates_json"])
                best = next((i for i, item in enumerate(candidates) if item.get("verified")), None)
                if best is None:
                    continue
                tasks.enqueue_processing(connection, row["id"], best, target_language)
                sent += 1
            manager.pump()
            if sent:
                st.rerun()
        if not rows:
            st.info("Finding is empty. Add links or photos on the Add video page.")
            return
        for row in rows:
            card = json.loads(row["card_json"] or "{}")
            label = (card.get("title") if row["kind"] == "card" else
                     Path(row["source"]).name if row["kind"] == "photo" else row["source"])
            with st.container(border=True):
                st.subheader(label or "Video", anchor=False)
                st.caption(f"{row['kind']} · {row['status']} · {row['stage']}")
                st.progress(float(row["progress"]), text=row["stage"])
                if st.button("Delete from Finding", key=f"delete-find-{row['id']}",
                             help="Remove this video from the queue without processing it"):
                    tasks.skip_finding(connection, row["id"])
                    st.rerun()
                if row["kind"] == "card" and row["photo_path"]:
                    st.image(photo_region(row["photo_path"], card.get("bbox")), width=260)
                    st.caption(" · ".join(str(card.get(key, "")) for key in
                                          ("author", "duration", "position") if card.get(key)))
                if row["status"] == "failed":
                    st.error(row["error"] or "Finding failed")
                    if st.button("Retry finding", key=f"retry-find-{row['id']}"):
                        tasks._update(connection, "finding_tasks", row["id"],
                                      status="queued", stage="queued", progress=0,
                                      detail="Retry requested")
                        manager.pump()
                        st.rerun()
                if row["status"] == "ready":
                    candidates = json.loads(row["candidates_json"])
                    if candidates:
                        choices = list(range(len(candidates))) + [None]
                        default = next((i for i, item in enumerate(candidates)
                                        if item.get("verified")), len(candidates))
                        chosen = st.selectbox(
                            "Choose original video", choices, index=default,
                            format_func=lambda index, found=candidates: (
                                "Skip this video" if index is None else
                                f"{'✓ AI match' if found[index].get('verified') else 'AI mismatch'} · "
                                f"{found[index]['title']} · {found[index].get('author','')}"),
                            key=f"candidate-{row['id']}")
                        with st.expander("Why these results matched or did not match"):
                            for item in candidates:
                                st.write(f"{item['title']}: {item.get('reason','')}")
                        if st.button("Process", key=f"process-{row['id']}", disabled=chosen is None):
                            tasks.enqueue_processing(connection, row["id"], chosen, target_language)
                            manager.pump()
                            st.rerun()
    finally:
        connection.close()


@st.fragment(run_every=2)
def processing_panel() -> None:
    manager.pump()
    connection = store.connect(DB)
    try:
        rows = [dict(row) for row in connection.execute("""
          SELECT p.*,v.title FROM processing_tasks p JOIN videos v ON v.id=p.video_id
          WHERE p.status!='skipped'
          ORDER BY p.created_at DESC,p.id DESC""")]
        if not rows:
            st.info("Processing is empty. Choose a found video and click Process.")
            return
        for row in rows:
            with st.container(border=True):
                st.subheader(row["title"], anchor=False)
                st.caption(f"{row['status']} · {row['stage']}")
                st.progress(float(row["progress"]), text=row["stage"])
                if st.button("Delete from Processing", key=f"delete-process-{row['id']}",
                             help="Remove this task and cancel remaining steps after the current operation finishes. Completed Library summaries are kept."):
                    tasks.skip_processing(connection, row["id"])
                    st.rerun()
                latest = connection.execute("""SELECT detail FROM task_events
                  WHERE task_type='processing' AND task_id=? ORDER BY id DESC LIMIT 1""",
                  (row["id"],)).fetchone()
                if latest:
                    st.caption(latest["detail"])
                if row["status"] == "failed":
                    st.error(row["error"] or "Processing failed")
                    if st.button("Retry processing", key=f"retry-process-{row['id']}"):
                        tasks._update(connection, "processing_tasks", row["id"],
                                      status="queued", stage="queued", progress=0,
                                      detail="Retry requested")
                        manager.pump()
                        st.rerun()
                elif row["status"] == "ready":
                    st.success("Summary ready in Library")
    finally:
        connection.close()


with find_tab:
    finding_panel()

with process_tab:
    processing_panel()

with library_tab:
    videos = [dict(row) for row in db.execute("SELECT * FROM videos WHERE status!='cancelled'")]
    if not videos:
        st.info("Completed videos will appear here.")
    else:
        counts = {}
        for item in videos:
            category = item["category"] or ""
            counts[category] = counts.get(category, 0) + 1
        types = ["__all__"] + sorted(counts, key=presentation.category_label)
        type_col, date_col, order_col = st.columns(3)
        selected_type = type_col.selectbox(
            "Video type", types,
            format_func=lambda category: (
                f"All types ({len(videos)})" if category == "__all__" else
                f"{presentation.category_label(category)} ({counts[category]})"))
        date_field = date_col.selectbox(
            "Sort date", ["published_at", "created_at"],
            format_func=lambda field: ("Video date" if field == "published_at"
                                       else "Date added"))
        order = order_col.selectbox("Order", ["Latest first", "Oldest first"])
        if selected_type != "__all__":
            videos = [item for item in videos
                      if (item["category"] or "") == selected_type]
        videos = presentation.order_library(
            videos, date_field=date_field, newest_first=order == "Latest first")
        options = {f"{item['title']} · {item['status']} · {item['id'][:6]}": item["id"]
                   for item in videos}
        chosen_label = st.selectbox("Video", list(options), index=next(
            (i for i, value in enumerate(options.values())
             if value == st.session_state.get("selected")), 0))
        video_id = options[chosen_label]
        video = store.detail(db, video_id)
        st.subheader(video["title"])
        st.write(f"{video['author']} · {video['published_at'] or 'Date unknown'} · "
                 f"{presentation.category_label(video['category'])}")
        st.link_button("Open original video", video["url"])
        if video["source_photo"] and Path(video["source_photo"]).is_file():
            with st.expander("Source photo"):
                st.image(video["source_photo"])
        if video["status"] == "ready":
            st.html(presentation.summary_html(video, font_size=sizes[size_choice]))
            with st.expander("Full transcript"):
                for group in presentation.merge_transcript(video["segments"]):
                    st.write(f"{presentation.timestamp(group['start'])}–"
                             f"{presentation.timestamp(group['end'])}  {group['text']}")
            translated_rows = [{**row, "text": row["translated_text"]}
                               for row in video["segments"] if row["translated_text"]]
            if translated_rows:
                with st.expander("Translated transcript"):
                    for group in presentation.merge_transcript(translated_rows):
                        st.write(f"{presentation.timestamp(group['start'])}–"
                                 f"{presentation.timestamp(group['end'])}  {group['text']}")
            if video["frames"]:
                with st.expander("Screenshots and visual text"):
                    for frame in video["frames"]:
                        st.image(frame["image_path"],
                                 caption=presentation.timestamp(frame["at_seconds"]))
                        st.write(frame["extracted_text"] or "No readable visual text")
            with st.form("feedback"):
                rating = st.slider("How useful was this summary?", 1, 5, 4)
                note = st.text_input("What should improve next time?")
                if st.form_submit_button("Save feedback"):
                    pipeline.feedback(db, video_id, rating, note)
                    st.success("Feedback saved")
            if st.button("Refresh summary using latest feedback"):
                try:
                    with intelligence.usage_context(DB, video_id=video_id):
                        pipeline.resummarize(db, video_id, skill_file=SKILL)
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))
            existing_frames = {frame["segment_id"] for frame in video["frames"]}
            missing_visuals = [row for row in pipeline._visual_cues(video["segments"])
                               if row["id"] not in existing_frames]
            visual_gap = bool(missing_visuals) and any(
                step["stage"] == "visual warning" for step in video["events"])
            active_repair = db.execute("""SELECT 1 FROM processing_tasks
              WHERE video_id=? AND mode='repair_visual' AND status IN ('queued','running')
              LIMIT 1""", (video_id,)).fetchone()
            if visual_gap and st.button("Retry missing screenshots",
                                        disabled=bool(active_repair)):
                tasks.enqueue_processing_video(db, video_id, mode="repair_visual")
                manager.pump()
                st.success("Screenshot retry sent to Processing")
        else:
            st.caption(f"Current stage: {video['stage']}")
            if video["error"]:
                st.error(video["error"])
            active = db.execute("""SELECT 1 FROM processing_tasks WHERE video_id=?
              AND status IN ('queued','running') LIMIT 1""", (video_id,)).fetchone()
            if not active and st.button("Queue this video for processing"):
                tasks.enqueue_processing_video(db, video_id)
                manager.pump()
                st.rerun()
        with st.expander("Processing trace"):
            for step in video["events"]:
                st.write(f"{step['at']} · {step['stage']} · {step['detail']}")

with measure_tab:
    st.subheader("AI token usage")
    st.caption("Measured from API responses since usage tracking was enabled. "
               "Earlier calls cannot be reconstructed from the video records.")
    period = st.selectbox("Period", ["7 days", "30 days", "All time"])
    since = None if period == "All time" else (
        datetime.now(timezone.utc) - timedelta(days=7 if period == "7 days" else 30)
        ).isoformat(timespec="seconds")
    usage_rows = [dict(row) for row in db.execute("""
      SELECT u.*,v.title,v.category FROM ai_usage u
      LEFT JOIN videos v ON v.id=u.video_id
      WHERE (? IS NULL OR u.at>=?) ORDER BY u.at DESC,u.id DESC""", (since, since))]
    if not usage_rows:
        st.info("No measured AI calls in this period yet.")
    else:
        usage = usage_visuals.prepare_usage(usage_rows)
        total_tokens = int(usage["total_tokens"].sum())
        low = float(usage["estimated_cost_low_usd"].fillna(0).sum())
        high = float(usage["estimated_cost_high_usd"].fillna(0).sum())
        a, b, c, d = st.columns(4)
        a.metric("AI calls", len(usage))
        b.metric("Total tokens", f"{total_tokens:,}")
        c.metric("Input / output", f"{int(usage['prompt_tokens'].sum()):,} / "
                 f"{int(usage['completion_tokens'].sum()):,}")
        d.metric("Estimated cost range", f"${low:.6f} to ${high:.6f}")
        st.caption("Cost range uses published DeepSeek Flash off-peak and peak rates; "
                   "it is an estimate, not the provider's billed amount. "
                   "Other models have no cost estimate.")
        cost_daily = usage_visuals.complete_days(usage, [
            "estimated_cost_low_usd", "estimated_cost_high_usd"], period)
        st.altair_chart(usage_visuals.daily_bars(
            cost_daily, {"estimated_cost_low_usd": "Off-peak estimate",
                         "estimated_cost_high_usd": "Peak estimate"},
            title="Estimated USD cost by day", colors=["#fbbf24", "#fb923c"],
            money=True), width="stretch")
        token_daily = usage_visuals.complete_days(usage, [
            "prompt_tokens", "completion_tokens"], period)
        st.altair_chart(usage_visuals.daily_bars(
            token_daily, {"prompt_tokens": "Input", "completion_tokens": "Output"},
            title="Tokens by day", colors=["#7dd3fc", "#2563eb"]),
            width="stretch")
        for model, model_usage in usage.groupby("response_model", sort=True):
            st.markdown(f"#### {model}")
            left, right = st.columns(2)
            with left:
                calls = usage_visuals.complete_days(model_usage.assign(calls=1),
                                                    ["calls"], period)
                st.altair_chart(usage_visuals.daily_bars(
                    calls, {"calls": "API calls"}, title="Requests",
                    colors=["#3b82f6"]), width="stretch")
            with right:
                model_tokens = usage_visuals.complete_days(model_usage,
                                                           ["total_tokens"], period)
                st.altair_chart(usage_visuals.daily_bars(
                    model_tokens, {"total_tokens": "Tokens"}, title="Tokens",
                    colors=["#7dd3fc"]), width="stretch")
        st.markdown("#### Usage by task step")
        step_usage = usage_visuals.step_totals(usage)
        left, right = st.columns(2)
        with left:
            st.altair_chart(usage_visuals.category_bars(
                step_usage, "task_step", "tokens", title="API tokens by task step"),
                width="stretch")
        with right:
            st.altair_chart(usage_visuals.category_bars(
                step_usage, "task_step", "cost_high_usd",
                title="Estimated peak USD by task step", color="#fb923c", money=True),
                width="stretch")
        st.dataframe(step_usage.rename(columns={
            "task_step": "Step", "calls": "AI calls", "input_tokens": "Input tokens",
            "output_tokens": "Output tokens", "tokens": "Total tokens",
            "cost_low_usd": "Off-peak USD", "cost_high_usd": "Peak USD",
        }), hide_index=True)
        st.caption("Local OCR and ASR use no DeepSeek API tokens. AI steps include "
                   "photo identification, matching, translation, frame reading, "
                   "summary notes, final summary, and classification.")
        left, right = st.columns(2)
        with left:
            calls_by_step = step_usage[step_usage["calls"] > 0]
            st.altair_chart(usage_visuals.category_bars(
                calls_by_step, "task_step", "calls", title="AI calls by task step",
                color="#3b82f6"), width="stretch")
        with right:
            classified = usage.assign(category=usage["category"].fillna("Unclassified"))
            category_totals = classified.groupby("category", as_index=False)["total_tokens"].sum()
            st.altair_chart(usage_visuals.category_bars(
                category_totals, "category", "total_tokens",
                title="Tokens by video classification", color="#fbbf24"),
                width="stretch")
        per_video = usage[usage["video_id"].notna()].groupby(
            ["video_id", "title", "category"], dropna=False, as_index=False).agg(
                calls=("id", "count"), tokens=("total_tokens", "sum"),
                models=("response_model", lambda values: ", ".join(sorted(set(values)))),
                cost_low_usd=("estimated_cost_low_usd", "sum"),
                cost_high_usd=("estimated_cost_high_usd", "sum"),
                first_call=("at", "min"), last_call=("at", "max"))
        if not per_video.empty:
            st.write("Videos and classification")
            st.dataframe(per_video[["title", "category", "models", "calls", "tokens",
                                     "cost_low_usd", "cost_high_usd",
                                     "first_call", "last_call"]], hide_index=True)
        with st.expander("Individual AI calls"):
            usage["video_or_task"] = usage["title"].fillna(
                usage["finding_task_id"].fillna("—"))
            st.dataframe(usage[["local_time", "video_or_task", "purpose", "response_model",
                                "prompt_tokens", "completion_tokens", "total_tokens",
                                "cache_hit_tokens", "estimated_cost_low_usd",
                                "estimated_cost_high_usd"]], hide_index=True)
    with st.expander("Library quality measures"):
        st.json(metrics)

db.close()
