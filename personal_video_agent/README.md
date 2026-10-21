# Personal Video Agent

This is a new, single-person agent in its own folder. The earlier `via` project remains available. This agent accepts a video link or many screenshots at once. DeepSeek vision identifies each distinct video card in an image, including multiple cards in one photo. The app searches YouTube separately and asks DeepSeek to check each result against the identified card. You confirm one match per card. No metadata typing is required.

## Run

From this folder, with **64-bit Python 3.11 or later** and `ffmpeg` on `PATH`:

```powershell
python -m venv .venv64
.venv64\Scripts\python -m pip install -r requirements.txt
Copy-Item .env.example .env
# Edit .env with your OpenAI-compatible endpoint and model.
.venv64\Scripts\python -m streamlit run app.py
```

On this machine, the default `python` command is 32-bit. The working 64-bit executable is `C:\Users\10269\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe`; use it for the `-m venv .venv64` command if that is still your Python setup. Dependencies are already installed in this folder's `.venv64` environment.

Configure the endpoint and API key in this folder's `.env`, or supply them as environment variables. Paste your DeepSeek key after `PVA_LLM_API_KEY=`; keep `.env` private. The app does not read another project's `.env`. When the endpoint is `api.deepseek.com`, **every AI call uses `deepseek-flash`**, including identification, matching, translation, frame reading, summarization, and classification. The app stores `data/personal.db`, source photos, and selected screenshots locally. It sends images and transcript excerpts to the configured DeepSeek API. Keep Streamlit bound to localhost for this private, unauthenticated app.

## Processing

1. In **Add video**, paste multiple links (one per line) and/or upload multiple photos. **Find** clears the form and sends the inputs to **Finding**. Each complete video card identified in a photo becomes its own finding task with a progress bar. Local OCR is only a reading aid. The app searches YouTube for each card; DeepSeek checks candidates without searching. Confirm a match and click **Process**; that video moves to **Processing** with its own progress bar. **Delete from Finding** removes one unprocessed video from the queue, leaving other cards from the same photo available. Up to two finding tasks and two processing tasks can run concurrently. Failed tasks show their errors, can be retried, and do not stop the queue.
2. Try available captions. If none are accessible, download media and transcribe locally with faster-whisper.
3. Pick a summary language from the optional sidebar menu: English, 简体中文, 繁體中文, Español, or 粵語. Prefer captions in that language. Translate timestamped segments only when the caption or ASR language differs, including a distinction between simplified Chinese, traditional Chinese, and Cantonese.
4. When speech refers to a chart, figure, image, table, slide, or flow, download a small visual copy if needed, retain selected frames, and read them with OCR and/or DeepSeek vision. Temporary media is deleted after processing. Transient SSL download failures get a bounded retry. For a completed video with a visual warning, **Retry missing screenshots** in Library uses the stored transcript and then refreshes the summary.
5. Summarize from transcript and visual evidence under `summary-skill/SKILL.md`, leading with the central message and why it matters; then classify from the completed summary.
6. Save transcript, translations, claims, evidence IDs, screenshots, status, and stage events in SQLite. The Library page expands them for review.

The model must return structured JSON and every displayed point must reference stored evidence. Library shows a clean summary first; claims and evidence, full transcript, translated transcript, screenshots, and processing trace are optional expanders. You can filter Library by the video types present and order by publication date or date added, latest or oldest first. The classifier recognizes tech, medical, finance, politics, religion, music, lifestyle, sports, health, and other types. The sidebar offers Standard, Large, and Extra large reading text. Adjacent sliding captions are merged into readable timed passages. Refreshing a completed summary uses stored evidence and feedback; it does not re-download or re-transcribe. User ratings and notes are saved and included in future summary prompts.

## Usage dashboard

**Measure & learn** records each new AI call's time, task step, requested and returned model, video and category when known, input and output tokens, and cached input tokens. It shows narrow daily bars for cost, tokens, and each model's calls and tokens. Task-step charts and a compact table separate identification, matching, translation, visual reading, summary notes, final summary, and classification. A per-video total remains available. Local OCR and ASR display zero DeepSeek API tokens. All usage axes start at zero and displayed counts and costs are nonnegative. DeepSeek reports token counts in each API response. The displayed Flash dollar range uses the published off-peak and peak rates, so it is an estimate rather than a bill. Earlier API calls cannot be backfilled from existing summaries. [DeepSeek usage fields](https://api-docs.deepseek.com/api/create-chat-completion/) and [current model pricing](https://api-docs.deepseek.com/quick_start/pricing/) are the reference points.

## Current limits

- Photo search uses YouTube search via yt-dlp. If the original is elsewhere, paste its link.
- Sites may block metadata, captions, or media access. On YouTube sign-in or cookie-read errors, the default **Automatic** option tries local Firefox, Chrome, then Edge cookies; choosing one browser tries it first and then the others. Sign in to YouTube in the browser you want to use. The app does not store browser cookies. This depends on local browser access and cannot guarantee that YouTube will allow the request. If a visual-only download is blocked after captions are captured, the agent continues to a transcript-based summary and records a visual warning. If media is required for ASR and remains blocked, that task fails and the queue continues.
- Local RapidOCR reads text in photos and frames. A vision-capable model can add visual context when configured; if a frame cannot be read, its screenshot remains visible but the summary cannot cite unseen details.
- Finding and processing run in background worker pools. Their task status and progress persist in SQLite across app restarts.
- One local user, no login or network server hardening. Bind to localhost.

See [DEVELOPMENT_LOOP.md](DEVELOPMENT_LOOP.md) for the spec, implementation choices, verification, and next evaluation loop.
