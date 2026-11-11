"""Local Streamlit interface. Run: python -m streamlit run app.py --server.headless true"""
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace
from html import escape
from datetime import datetime, timedelta, timezone
from pathlib import Path

import streamlit as st
from PIL import Image

from pva import intelligence, llama_runtime, media, pipeline, presentation, store, tasks, usage_visuals
from pva.summary_router import SummaryRoute, list_models
from pva import library, library_ui, visual_evidence, uploads, web_sources


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DB = DATA / "personal.db"
SKILL = ROOT / "summary-skill" / "SKILL.md"
ASSETS = ROOT / "assets"


def photo_region(path: str, bbox: list | None):
    if not bbox:
        return path
    image = Image.open(path)
    width, height = image.size
    left, top, right, bottom = bbox
    return image.crop((int(left * width / 1000), int(top * height / 1000),
                       int(right * width / 1000), int(bottom * height / 1000)))


@st.cache_data
def artwork(name: str) -> str:
    return "data:image/webp;base64," + base64.b64encode((ASSETS / name).read_bytes()).decode("ascii")


st.set_page_config(page_title="Raccoon Agent", page_icon=str(ASSETS / "raccoon-logo.webp"),
                   layout="wide")
db = store.connect(DB)
manager = tasks.get_manager(DB, DATA, SKILL)
manager.pump()
prefs = dict(db.execute("SELECT key,value FROM preferences"))
languages = {"English": "en", "简体中文": "zh-Hans", "繁體中文": "zh-Hant",
             "Español": "es", "粵語": "yue"}
sizes = {"Normal": 24, "Larger": 28, "Largest": 32}
methods = {"auto": "Automatic", "direct": "Quick · single pass", "batched": "Detailed · batched"}
method_settings = {**methods, "ask": "Ask before processing"}
backends = {"llama.cpp": "Local llama.cpp", "api": "Custom API"}
api_providers = dict(sorted({
    "Alibaba Cloud (Qwen · Beijing)": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "Alibaba Cloud (Qwen · Singapore)": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    "Anthropic (Claude)": "https://api.anthropic.com/v1",
    "Baidu (Qianfan)": "https://qianfan.baidubce.com/v2",
    "Cerebras": "https://api.cerebras.ai/v1",
    "Cohere": "https://api.cohere.ai/compatibility/v1",
    "DeepSeek": "https://api.deepseek.com",
    "Fireworks AI": "https://api.fireworks.ai/inference/v1",
    "Google (Gemini)": "https://generativelanguage.googleapis.com/v1beta/openai",
    "Groq": "https://api.groq.com/openai/v1",
    "Kimi (Moonshot · China)": "https://api.moonshot.cn/v1",
    "Kimi (Moonshot · International)": "https://api.moonshot.ai/v1",
    "MiniMax (China)": "https://api.minimax.cn/v1",
    "MiniMax (International)": "https://api.minimax.io/v1",
    "Mistral AI": "https://api.mistral.ai/v1",
    "OpenAI": "https://api.openai.com/v1",
    "OpenRouter": "https://openrouter.ai/api/v1",
    "Other provider": "",
    "SiliconFlow (China)": "https://api.siliconflow.cn/v1",
    "SiliconFlow (International)": "https://api.siliconflow.com/v1",
    "Tencent (Hunyuan)": "https://api.hunyuan.cloud.tencent.com/v1",
    "Together AI": "https://api.together.ai/v1",
    "Volcengine (Doubao)": "https://ark.cn-beijing.volces.com/api/v3",
    "xAI (Grok)": "https://api.x.ai/v1",
    "Z.AI (GLM · International)": "https://api.z.ai/api/paas/v4",
    "Zhipu AI (GLM · China)": "https://open.bigmodel.cn/api/paas/v4",
}.items(), key=lambda item: item[0].casefold()))
intelligence._load_env()
saved_backend = prefs.get("summary_backend", "llama.cpp")
size_choice = prefs.get("reading_size", "Normal")
if size_choice not in sizes:
    size_choice = "Normal"
target_language = prefs.get("summary_language", "en")
summary_method = prefs.get("summary_method", "auto")
if summary_method not in method_settings:
    summary_method = "auto"
ask_summary_method = summary_method == "ask"
summary_route = SummaryRoute(
    backend=saved_backend if saved_backend in backends else "llama.cpp",
    method="auto" if ask_summary_method else summary_method,
    base_url=prefs.get("summary_base_url", ""), model=prefs.get("summary_model", ""),
)


def save_preferences(values: dict[str, str]) -> None:
    with db:
        db.executemany("""INSERT INTO preferences(key,value) VALUES(?,?)
          ON CONFLICT(key) DO UPDATE SET value=excluded.value""", values.items())


def choose_summary_method(key: str, *, batch: bool = False) -> str | None:
    if not ask_summary_method:
        return summary_route.method
    return st.selectbox(
        "Summary method for this batch" if batch else "Summary method for this task",
        list(methods), index=None, format_func=methods.get, key=f"summary_method_{key}",
        placeholder="Choose a method before processing",
        help="Automatic adapts to length. Quick uses one pass when possible. Detailed collects "
             "evidence in batches. Change your default in Settings → Summary engine.",
    )


def selected_summary_route(method: str | None, route: SummaryRoute = summary_route) -> SummaryRoute | None:
    if method is None:
        st.warning("Choose a summary method before processing.")
        return None
    return replace(route, method=method)


def queue_summary(action, *args, method: str | None, route: SummaryRoute = summary_route, **kwargs) -> bool:
    selected = selected_summary_route(method, route)
    if selected is None:
        return False
    try:
        action(*args, summary_route=selected, **kwargs)
    except ValueError as exc:
        st.error(str(exc))
        return False
    manager.pump()
    return True


def show_saved(message: str) -> None:
    st.html(f"""<div class="raccoon-saved" role="status">
      <img src="{artwork('raccoon-ok.webp')}" alt="Raccoon making an OK gesture">
      <span>{escape(message)}</span>
    </div>""")


def navigate(page: str, message: str = "", *, saved: bool = False) -> None:
    st.session_state["raccoon_next_page"] = page
    if message:
        st.session_state["raccoon_message"] = (message, saved)
    st.rerun()


st.html(ASSETS / "raccoon.css")
st.html(f"""<style>
.stApp {{ --reading-size: {sizes[size_choice]}px; }}
</style>
<header class="raccoon-brand">
  <img src="{artwork('raccoon-logo.webp')}" alt="" width="68" height="68">
  <h1>Raccoon Agent</h1>
  <p>Summarize videos, audio, documents and webpages with source evidence.</p>
</header>""")
if next_page := st.session_state.pop("raccoon_next_page", None):
    st.session_state["raccoon_page"] = next_page
page = st.session_state.get("raccoon_page", "Add sources")
if page == "Add videos":  # Migrate sessions open before the navigation rename.
    page = st.session_state["raccoon_page"] = "Add sources"
with st.container(key="raccoon_navigation", horizontal=True, horizontal_alignment="center"):
    for destination, key in (("Add sources", "add"), ("Raccoon activity", "activity"),
                             ("Library", "library"), ("Settings", "settings")):
        if st.button(destination, key=f"nav_{key}", type="primary" if page == destination else "secondary"):
            if destination == "Library":
                st.session_state.pop("selected", None)
            navigate(destination)
if notice := st.session_state.pop("raccoon_message", None):
    message, saved = notice
    if saved:
        show_saved(message)
    else:
        st.success(message)
for error in st.session_state.pop("raccoon_batch_errors", []):
    st.error(error)
for path in st.session_state.pop("raccoon_cleanup_warnings", []):
    st.warning(f"Could not remove the cached file: {path}")


def settings_page() -> None:
    st.header("Settings", anchor=False)
    st.subheader("Summary engine", anchor=False)
    local_installed = llama_runtime.available()
    try:
        SummaryRoute(backend="api", base_url=summary_route.base_url,
                     model=summary_route.model).register()
        api_configured = True
    except ValueError:
        api_configured = False
    labels = {
        "llama.cpp": f"Local llama.cpp ({'Installed' if local_installed else 'Not installed'})",
        "api": f"Custom API ({'Configured' if api_configured else 'Not configured'})",
    }
    backend = st.radio("Where should summaries be written?", list(backends),
                       index=list(backends).index(summary_route.backend), format_func=labels.get,
                       horizontal=True)
    base_url, api_key, model = "", "", ""
    provider = None
    if backend == "api":
        def clear_api_key() -> None:
            st.session_state.pop("summary_api_key_input", None)

        def clear_api_draft() -> None:
            clear_api_key()
            st.session_state.pop("summary_api_model_input", None)
            st.session_state.pop("summary_api_url_input", None)

        if st.session_state.pop("clear_summary_api_key", False):
            clear_api_key()
        saved_url = summary_route.base_url.rstrip("/")
        saved_provider = next((name for name, url in api_providers.items() if url and url == saved_url), None)
        if saved_url == "https://api.deepseek.com/v1":
            saved_provider = "DeepSeek"
        if saved_url and saved_provider is None:
            saved_provider = "Other provider"
        providers = list(api_providers)
        provider = st.selectbox("API provider", providers,
                                index=providers.index(saved_provider) if saved_provider else None,
                                placeholder="Choose your API provider",
                                key="summary_api_provider_input", on_change=clear_api_draft,
                                help="Listed A–Z. Choose the region that issued your API key. "
                                     "Use Other provider for a private endpoint or an unlisted service.")
        if provider == "Other provider":
            def on_url_change() -> None:
                clear_api_key()
                st.session_state.pop("summary_api_model_input", None)

            base_url = st.text_input("API base URL", value=saved_url if provider == saved_provider else "",
                                     placeholder="https://your-provider.example/v1",
                                     key="summary_api_url_input", on_change=on_url_change)
        elif provider:
            base_url = saved_url if provider == saved_provider else api_providers[provider]
        same_endpoint = bool(saved_url) and base_url.strip().rstrip("/") == saved_url
        if provider:
            api_key = st.text_input("API key", type="password", key="summary_api_key_input",
                                    help="The key is held in memory. Enter it again after the app restarts.")
        if base_url.strip() and (provider == "Other provider" or api_key.strip() or
                                 (same_endpoint and api_configured)):
            lookup = (base_url.strip().rstrip("/"), hashlib.sha256(api_key.strip().encode()).digest(),
                      summary_route.model)
            refresh_models = st.button("Refresh model list")
            if st.session_state.get("summary_model_lookup") != lookup or refresh_models:
                try:
                    models = list_models(base_url, api_key, saved_model=summary_route.model)
                    message = "" if models else "This API returned an empty model list."
                except (ValueError, RuntimeError) as exc:
                    models, message = [], str(exc)
                st.session_state.update(summary_model_lookup=lookup,
                                        summary_api_models=models, summary_api_model_message=message)
            models = st.session_state["summary_api_models"]
            if models:
                picker_key = "summary_api_suggestion_" + hashlib.sha256(lookup[0].encode()).hexdigest()

                def copy_suggestion() -> None:
                    model_id = st.session_state[picker_key]
                    if model_id is not None:
                        st.session_state["summary_api_model_input"] = model_id

                st.selectbox("Available models", models, index=None, placeholder="Choose a suggested model",
                             key=picker_key, on_change=copy_suggestion)
            elif message := st.session_state["summary_api_model_message"]:
                st.caption(message + " You can enter a model ID manually.")
        if provider:
            model = st.text_input("API model name", value=summary_route.model if same_endpoint else "",
                                  placeholder="Select above or enter a model ID",
                                  key="summary_api_model_input")

    with st.form("summary_settings"):
        method = st.selectbox("How should the agent summarize?", list(method_settings),
                              index=list(method_settings).index(summary_method), format_func=method_settings.get)
        st.caption("Automatic adapts to transcript length. Quick writes in one pass when the transcript fits. "
                   "Detailed collects evidence in batches before writing. Ask before processing lets you "
                   "choose a method for each task or batch.")
        if backend == "api":
            st.caption("The selected provider receives transcript excerpts and any extracted visual text "
                       "needed to write the summary. Provider charges may apply.")
        elif not local_installed:
            st.warning("Install the local runtime and model with setup-local-llama.ps1 first.")
        if st.form_submit_button("Save summary settings", type="primary"):
            try:
                if backend == "api" and not provider:
                    raise ValueError("Choose an API provider.")
                chosen = SummaryRoute(backend=backend, method="auto" if method == "ask" else method, base_url=base_url,
                                      model=model, api_key=api_key).register()
                chosen["method"] = method
                if backend != "api":
                    chosen = {key: chosen[key] for key in ("backend", "method")}
                save_preferences({f"summary_{key}": value for key, value in chosen.items()})
                if backend == "api":
                    st.session_state["clear_summary_api_key"] = True
                navigate("Settings", "Summary settings saved for new jobs.", saved=True)
            except ValueError as exc:
                st.error(str(exc))
    st.subheader("Reading and YouTube", anchor=False)
    saved_browser = tasks.cookie_browser(db)
    browsers = ["auto", "firefox", "chrome", "edge", ""]
    with st.form("reading_settings"):
        language_choice = st.selectbox("Summary language", list(languages),
            index=next((i for i, code in enumerate(languages.values()) if code == target_language), 0))
        selected_size = st.selectbox("Reading text size", list(sizes), index=list(sizes).index(size_choice))
        cookie_choice = st.selectbox("YouTube login browser", browsers,
            index=browsers.index(saved_browser) if saved_browser in browsers else 0,
            format_func=lambda value: ("Automatic (Firefox, Chrome, Edge)" if value == "auto"
                                       else value.title() if value else "No browser cookies"),
            help="If YouTube asks for sign-in, the app tries cookies from your selected browser first.")
        if st.form_submit_button("Save reading and browser settings"):
            save_preferences({"summary_language": languages[language_choice],
                              "reading_size": selected_size, "youtube_cookie_browser": cookie_choice})
            navigate("Settings", "Reading and browser settings saved.", saved=True)

    st.subheader("Storage location", anchor=False)
    st.write("Raccoon Agent creates a folder named `data` beside the app on first run. "
             "Your library, uploads, and saved screenshots are stored there.")
    st.code(str(DATA), language=None)
    st.caption(f"Library database · {DB.name}")

if page == "Settings":
    settings_page()

if page == "Add sources":
    st.header("Add sources", anchor=False)
    st.html("""<div class="raccoon-steps" aria-label="How it works">
      <div><b>1</b><span>Add links or photos</span></div>
      <div><b>2</b><span>Confirm and process</span></div>
      <div><b>3</b><span>Read in Library</span></div>
    </div>""")
    intake, illustration = st.columns([1.55, 1], gap="medium")
    with illustration:
        st.html(f"""<aside class="raccoon-illustration">
          <img src="{artwork('raccoon-tv.webp')}" alt="A cartoon raccoon watching a television">
          <p><span>Send me a video.</span><span>I’ll find what matters.</span></p>
        </aside>""")
    with intake.form("add_inputs", clear_on_submit=True):
        links = st.text_area("Video links (one per line)", height=125,
                             placeholder="https://www.youtube.com/watch?v=...")
        photos = st.file_uploader("Photos or screenshots", type=["jpg", "jpeg", "png"],
                                  accept_multiple_files=True,
                                  help="Upload several images at once. No title or author needed.")
        submitted = st.form_submit_button("Find videos", type="primary")
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
        if queued and not errors:
            st.session_state["raccoon_activity_next"] = "Finding"
            navigate("Raccoon activity", f"Sent {queued} input(s) to Finding. Confirm a match to start a summary.")
        elif queued:
            st.success(f"Sent {queued} input(s) to Finding in Raccoon activity.")
        elif not errors:
            st.info("Paste a link or upload a photo first.")
        for error in errors:
            st.error(error)
    st.subheader("Audio and documents", anchor=False)
    with st.form("upload_sources", clear_on_submit=not ask_summary_method):
        files = st.file_uploader("Upload audio or documents", type=uploads.EXTENSIONS,
                                  accept_multiple_files=True,
                                  help="Audio: MP3, WAV, M4A, AAC, FLAC, OGG, OPUS, WMA. "
                                       "Documents: PDF (including scans), DOCX, TXT, MD.")
        st.caption("Files go directly to Processing. Audio is transcribed locally; document text "
                   "is summarized in your selected language. Scanned pages and document images use "
                   "your configured visual reader; API OCR may incur charges.")
        file_method = choose_summary_method("uploads", batch=True)
        process_files = st.form_submit_button("Process uploaded files", type="primary")
    if process_files and (file_route := selected_summary_route(file_method)) is not None:
        queued, errors = 0, []
        for source_file in files or []:
            try:
                uploads.save(db, DATA, source_file.name, source_file.getvalue(),
                             target_language, file_route)
                queued += 1
            except Exception as exc:
                errors.append(f"{source_file.name}: {exc}")
        manager.pump()
        if queued and (not errors or ask_summary_method):
            st.session_state["raccoon_batch_errors"] = errors
            st.session_state["raccoon_activity_next"] = "Processing"
            navigate("Raccoon activity", f"Sent {queued} file(s) to Processing.")
        if queued:
            st.success(f"Sent {queued} file(s) to Processing.")
        if not files:
            st.info("Upload an audio or document file first.")
        for error in errors:
            st.error(error)
    st.subheader("Webpages and PDF links", anchor=False)
    with st.form("web_sources", clear_on_submit=not ask_summary_method):
        webpage_urls = st.text_area("Webpage URLs (one per line)",
                                   placeholder="https://example.com/article")
        st.caption("Paste multiple public webpages or direct PDF URLs, one per line. Each URL gets its own task. "
                   "Pages requiring login or JavaScript may not be readable.")
        web_method = choose_summary_method("webpages", batch=True)
        import_web = st.form_submit_button("Process webpages", type="primary")
    if import_web and (web_route := selected_summary_route(web_method)) is not None:
        queued, errors = 0, []
        for url in dict.fromkeys(line.strip() for line in webpage_urls.splitlines() if line.strip()):
            try:
                web_sources.enqueue(db, url, target_language, web_route)
                queued += 1
            except Exception as exc:
                errors.append(f"{url}: {exc}")
        manager.pump()
        if queued and (not errors or ask_summary_method):
            st.session_state["raccoon_batch_errors"] = errors
            st.session_state["raccoon_activity_next"] = "Processing"
            navigate("Raccoon activity", f"Sent {queued} webpage(s) to Processing.")
        if queued:
            st.success(f"Sent {queued} webpage(s) to Processing.")
        if not queued and not errors:
            st.info("Paste a webpage URL first.")
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
        batch_method = choose_summary_method("verified_batch", batch=True) if ready else None
        if ready and st.button("Process all AI verified videos", key="process_all", disabled=batch_method is None):
            sent = 0
            for row in ready:
                candidates = json.loads(row["candidates_json"])
                best = next((i for i, item in enumerate(candidates) if item.get("verified") is True), None)
                if best is None:
                    continue
                if not queue_summary(tasks.enqueue_processing, connection, row["id"], best, target_language,
                                     method=batch_method):
                    break
                sent += 1
            if sent:
                st.session_state["raccoon_activity_next"] = "Processing"
                navigate("Raccoon activity", f"Sent {sent} video(s) to Processing.")
        if not rows:
            st.info("No videos to confirm yet. Start by adding a link or photo.")
            if st.button("Add links or photos", key="finding_add"):
                navigate("Add sources")
            return
        for row in rows:
            card = json.loads(row["card_json"] or "{}")
            label = (card.get("title") if row["kind"] == "card" else
                     Path(row["source"]).name if row["kind"] == "photo" else row["source"])
            with st.container(border=True, key=f"finding_card_{row['id']}"):
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
                        if not any(item.get("verified") is True for item in candidates):
                            st.warning("No verified original video found. Add a clearer photo or the original video link.")
                        choices = list(range(len(candidates))) + [None]
                        default = next((i for i, item in enumerate(candidates)
                                        if item.get("verified") is True), len(candidates))
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
                        verified_choice = chosen is not None and candidates[chosen].get("verified") is True
                        video_method = choose_summary_method(f"video_{row['id']}") if verified_choice else None
                        if st.button("Process video", key=f"process-{row['id']}", type="primary",
                                     disabled=not verified_choice or video_method is None,
                                     help="A verified original video is required before processing"):
                            if queue_summary(tasks.enqueue_processing, connection, row["id"], chosen, target_language,
                                             method=video_method):
                                st.session_state["raccoon_activity_next"] = "Processing"
                                navigate("Raccoon activity", "Video sent to Processing.")
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
            with st.container(border=True, key=f"processing_card_{row['id']}"):
                st.subheader(row["title"], anchor=False)
                st.caption(f"{row['status']} · {row['stage']}")
                route = json.loads(row.get("summary_route_json") or "{}")
                st.caption("Summary · " + backends.get(route.get("backend"), "Legacy local engine") +
                           " · " + methods.get(route.get("method"), methods["auto"]))
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
                    retry_method = choose_summary_method(f"retry_{row['id']}")
                    if route.get("method") == "direct" and not ask_summary_method:
                        st.caption("Automatic can split long transcripts into batches.")
                        if st.button("Retry with Automatic", key=f"retry-auto-{row['id']}"):
                            try:
                                tasks.retry_processing(connection, row["id"],
                                    summary_route=SummaryRoute(**{**route, "method": "auto"}))
                                manager.pump()
                                st.rerun()
                            except ValueError as exc:
                                st.error(str(exc))
                    if st.button("Retry processing", key=f"retry-process-{row['id']}", disabled=retry_method is None):
                        if ask_summary_method:
                            if queue_summary(tasks.retry_processing, connection, row["id"], method=retry_method,
                                             route=SummaryRoute(**route)):
                                st.rerun()
                        else:
                            tasks.retry_processing(connection, row["id"])
                            manager.pump()
                            st.rerun()
                    if route.get("backend", "local") != summary_route.backend and st.button(
                        f"Retry with {backends[summary_route.backend]}", key=f"retry-route-{row['id']}",
                        disabled=retry_method is None,
                    ):
                        if queue_summary(tasks.retry_processing, connection, row["id"], method=retry_method):
                            st.rerun()
                elif row["status"] == "ready":
                    st.success("Summary ready in Library")
                    if st.button("Read summary", key=f"open-library-{row['id']}"):
                        st.session_state["selected"] = row["video_id"]
                        navigate("Library")
    finally:
        connection.close()


activity = None
if page == "Raccoon activity":
    st.header("Raccoon activity", anchor=False)
    st.write("Confirm the original in Finding. Follow each video in Processing.")
    if next_activity := st.session_state.pop("raccoon_activity_next", None):
        st.session_state["raccoon_activity"] = next_activity
    activity = st.radio("Activity view", ["Finding", "Processing", "History", "Usage"],
                        horizontal=True, label_visibility="collapsed", key="raccoon_activity")
    if activity == "Finding":
        st.caption(f"New summaries · {backends[summary_route.backend]} · {method_settings[summary_method]}")
        if st.button("Change summary settings"):
            navigate("Settings")
        finding_panel()
    elif activity == "Processing":
        processing_panel()
    elif activity == "History":
        library_ui.history_panel(db, DB, manager, choose_summary_method, queue_summary, target_language)

if page == "Library":
    st.header("Your library", anchor=False)
    rows = library.entries(db)
    entry = next((item for item in rows if item["id"] == st.session_state.get("selected")), None)
    if entry is None:
        library_ui.listing(db, DB, rows, manager)
    else:
        if st.button("← Back to library"):
            st.session_state.pop("selected", None)
            st.rerun()
        video_id = entry["id"]
        video = store.detail(db, video_id)
        names, memberships = library.collection_state(db)
        folders = [names[key] for key in names if key in memberships.get(video_id, ())]
        st.subheader(video["title"], anchor=False)
        st.caption(library_ui.metadata(entry, folders))
        if entry["status"] == "ready":
            with st.container(horizontal=True, gap="small"):
                if st.button("Export / share", key="detail_export"):
                    library_ui.export_summary(DB, entry)
                if st.button("Collections", key="detail_collections"):
                    library_ui.edit_collections(DB, entry)
                if st.button("Delete summary", key="detail_delete"):
                    library_ui.confirm_remove(manager, video_id, video["title"])
        uploaded_source = db.execute("SELECT * FROM uploaded_sources WHERE video_id=?",
                                     (video_id,)).fetchone()
        is_document = uploaded_source is not None and uploaded_source["kind"] in {"document", "webpage"}
        if uploaded_source is None or uploaded_source["kind"] == "webpage":
            st.link_button("Open original webpage" if uploaded_source else "Open original video", video["url"])
        if uploaded_source is not None and Path(uploaded_source["path"]).is_file():
            with open(uploaded_source["path"], "rb") as original:
                download_name = ("webpage" + Path(uploaded_source["path"]).suffix
                                 if uploaded_source["kind"] == "webpage" else video["title"])
                st.download_button("Download original file", original, file_name=download_name)
        if video["source_photo"] and Path(video["source_photo"]).is_file():
            with st.expander("Source photo"):
                st.image(video["source_photo"])
        if video["status"] == "ready":
            st.caption("Select a little TV beside a point to see its evidence.")
            st.html(presentation.summary_html(video, font_size=sizes[size_choice]))
            with st.expander("Full document text" if is_document else "Full transcript"):
                if is_document:
                    for index, group in enumerate(video["segments"], 1):
                        st.caption(f"Section {index}")
                        st.text(group["text"])
                else:
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
            library_method = choose_summary_method(f"refresh_{video_id}")
            if video["frames"]:
                with st.expander("Document images and visual text" if is_document else "Screenshots and visual text"):
                    for frame in video["frames"]:
                        extracted = frame["extracted_text"] or "No readable visual text"
                        st.image(frame["image_path"],
                                 caption=extracted.splitlines()[0] if is_document else
                                 presentation.timestamp(frame["at_seconds"]))
                        st.html(visual_evidence.render(
                            extracted, html=True))
                active_visual = db.execute("""SELECT 1 FROM processing_tasks WHERE video_id=?
                  AND status IN ('queued','running') LIMIT 1""", (video_id,)).fetchone()
                if not is_document and st.button("Reread saved screenshots", disabled=bool(active_visual) or library_method is None,
                             help="Reads saved screenshots again and uses your chosen engine to refresh the summary."):
                    if queue_summary(tasks.enqueue_processing_video, db, video_id, mode="reread_visual",
                                     method=library_method):
                        st.session_state["raccoon_activity_next"] = "Processing"
                        navigate("Raccoon activity", "Saved screenshots sent to Processing.")
            with st.form("feedback"):
                rating = st.slider("How useful was this summary?", 1, 5, 4)
                note = st.text_input("What should improve next time?")
                if st.form_submit_button("Save feedback"):
                    pipeline.feedback(db, video_id, rating, note)
                    show_saved("Feedback saved.")
            if st.button("Refresh summary using latest feedback", disabled=library_method is None):
                if queue_summary(tasks.enqueue_processing_video, db, video_id, mode="resummarize",
                                 method=library_method):
                    st.session_state["raccoon_activity_next"] = "Processing"
                    navigate("Raccoon activity", "Summary refresh sent to Processing.")
            existing_frames = {frame["segment_id"] for frame in video["frames"]}
            missing_visuals = [row for row in pipeline._visual_cues(video["segments"])
                               if row["id"] not in existing_frames]
            visual_gap = not is_document and bool(missing_visuals) and any(
                step["stage"] == "visual warning" for step in video["events"])
            active_repair = db.execute("""SELECT 1 FROM processing_tasks
              WHERE video_id=? AND mode='repair_visual' AND status IN ('queued','running')
              LIMIT 1""", (video_id,)).fetchone()
            if visual_gap and st.button("Retry missing screenshots",
                                        disabled=bool(active_repair) or library_method is None):
                if queue_summary(tasks.enqueue_processing_video, db, video_id, mode="repair_visual",
                                 method=library_method):
                    st.session_state["raccoon_activity_next"] = "Processing"
                    navigate("Raccoon activity", "Screenshot retry sent to Processing.")
        else:
            st.caption(f"Current stage: {video['stage']}")
            if video["error"]:
                st.error(video["error"])
            active = db.execute("""SELECT 1 FROM processing_tasks WHERE video_id=?
              AND status IN ('queued','running') LIMIT 1""", (video_id,)).fetchone()
            if not active:
                ingest_method = choose_summary_method(f"ingest_{video_id}")
            if not active and st.button("Queue this item for processing", disabled=ingest_method is None):
                if queue_summary(tasks.enqueue_processing_video, db, video_id, method=ingest_method):
                    st.session_state["raccoon_activity_next"] = "Processing"
                    navigate("Raccoon activity", "Item sent to Processing.")
        with st.expander("Processing trace"):
            for step in video["events"]:
                st.write(f"{step['at']} · {step['stage']} · {step['detail']}")

if page == "Raccoon activity" and activity == "Usage":
    st.subheader("AI token usage")
    st.caption("Measured from model responses since usage tracking was enabled. "
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
        d.metric("Tracked cost estimate", f"${low:.6f} to ${high:.6f}")
        st.caption("The estimate covers DeepSeek Flash. Local inference costs $0 in API fees. "
                   "Custom summary API pricing is unavailable here; check your provider’s bill.")
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
                step_usage, "task_step", "tokens", title="Tokens by task step"),
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
        st.caption("Token counts include local models and provider usage. Summary steps use the engine "
                   "chosen for each job. Earlier calls remain in the totals.")
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
    st.markdown("#### Local picture and chart reading")
    local_reads = [dict(row) for row in db.execute("""SELECT l.at,l.model,l.device,l.purpose,
      l.seconds,l.status,l.cache_hit,l.output_chars,l.error,v.title
      FROM local_visual_usage l LEFT JOIN videos v ON v.id=l.video_id
      WHERE (? IS NULL OR l.at>=?) ORDER BY l.id DESC""", (since, since))]
    if local_reads:
        left, middle, right = st.columns(3)
        left.metric("Local visual reads", len(local_reads))
        middle.metric("Cache hits", sum(row["cache_hit"] for row in local_reads))
        right.metric("API cost / tokens", "$0 / 0")
        st.dataframe(local_reads, hide_index=True)
        st.caption("Elapsed seconds include model loading and first-use downloads. "
                   "Local compute uses your PC's resources; API usage is recorded separately above.")
    else:
        st.caption("Local PaddleOCR-VL runs will show model, device, time, cache hits and failures here.")
    with st.expander("Library quality measures"):
        st.json(pipeline.measures(db))

db.close()
