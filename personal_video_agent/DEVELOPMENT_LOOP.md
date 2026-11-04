# Development loop

## 1. Direction

Build a useful private agent for one person. Accept a video URL or multiple screenshots without typed metadata; identify each original video; gather spoken and relevant visual information; summarize in the user's language; classify; store and trace everything; let the user inspect and correct the result.

## 2. Specification

- DeepSeek vision separates every fully visible video card in each uploaded photo. Each identified card becomes a separate persistent finding task. The app searches candidate originals per card; DeepSeek checks the candidates without performing search. The user confirms a candidate and sends that task to Processing. Multiple links and photos can be submitted together; the Add form clears on Find.
- Captions win when accessible. Local ASR is the fallback. Every segment has start and end times.
- Target-language captions are preferred. Translation is skipped and recorded as such when the transcript language matches the target language.
- Visual reading is selective, driven by spoken references. Screenshots are retained; temporary downloaded media is removed.
- Claims refer to stored segment or frame IDs. The Library shows a briefing with the core message and importance, while evidence, merged transcript passages, screenshots, and processing trace stay in expanders.
- DeepSeek response usage is stored per call with task step, time, model, tokens, and video when known. A dashboard charts daily usage and shows separate cost and token totals by step, model, and video or finding task. Local OCR and ASR have zero DeepSeek API usage.
- SQLite remains the personal source of truth. Feedback is saved and used on future summary calls.

## 3. Plan and build

`pva.media` handles discovery and extraction; `pva.intelligence` handles model calls; `pva.pipeline` orchestrates stages; `pva.tasks` runs two finding and two processing workers with persistent status and progress; `pva.store` owns SQLite; `app.py` presents the five areas and feedback. The director's summary contract is in `summary-skill/SKILL.md`. All calls to `api.deepseek.com` use `deepseek-flash`. A failed task records its error and leaves the next queued task running.

## 4. Verify and repair

Run `python -m unittest discover -s tests -v` and `python -m compileall -q pva app.py`. Check the skill with the skill validator. A live video plus configured model is needed for a full external integration check; unit tests use deterministic fakes for the pipeline invariants.

Live discovery verified that one photo can create two separate video tasks and that exact-title, exact-creator search results can be matched independently. A caption check verified that a same-language track skips translation. A visual-only download and frame capture completed after transient SSL download settings were repaired. Tests cover photo splitting, failure continuation, language matching, clean summaries, transcript merging, usage recording, and visual repair.

## 5. Ship

The deliverable is this folder. Follow `README.md` to configure and launch locally. The app is not published or exposed on a public interface.

## 6. Measure, learn, evaluate

The Measure & learn tab charts provider-reported token usage by day, step, model, and video, and shows the estimated cost range. It also retains quality measures: ready and failed videos, videos with screenshots, traced claims, feedback count, average rating, and categories. Review failed-stage events and low-rated notes to decide the next repair. Refresh uses saved evidence and the latest feedback. Evaluate representative captioned, ASR, translated, and chart-heavy videos. Inspect whether each factual point's cited segment or frame supports it, whether the central message and importance are clear, and whether the category matches the final summary. Log misses as feedback, fix the stage responsible, and rerun that case.

## Local visual upgrade (2026-11-04)

- Direction: reduce paid frame-reading calls while retaining chart/table evidence and provenance.
- Specification: use the full PaddleOCR-VL 1.6 layout + recognition pipeline; enable chart recognition; keep DeepSeek Flash for photo-card identification, matching, translation and summarization. Paid visual fallback requires a saved opt-in.
- Build: separate `.venv-paddleocr`; bounded/cancellable worker; content-hash cache; local usage and failures recorded separately from provider tokens; sidebar backend choice.
- Verify: unit tests check chart blocks/bounds, no paid calls on local success or failure, explicit API fallback, caching, and Windows-safe worker cancellation. `tools/evaluate_local_vision.py` produces a real synthetic chart/table smoke check and records measured runtime and a repeated cache read under `data/vision-eval/`. Inspect actual output before claiming local recognition quality.
- Learn/evaluate: retain local runtime, model, device, failure and cache statistics. Do not infer that a 0.9B model is fast on this CPU or that OCR proves every chart relationship. Compare structured chart numbers with the source image.

Real evaluation: the full native CPU pipeline recognized a synthetic chart (Jan 20, Feb 35) and table (costs 10 and 12), with chart/table layout labels and no paid API calls. The first run took 408.891 seconds including model downloads and initialization; the cached repeat returned immediately. The cold-run figure is not a steady-state inference benchmark. See ignored data/vision-eval/ result files for this machine’s measurements.

Measured repair: feeding every text region of a crowded 480p interface screenshot to the VLM exceeded 600 seconds on this CPU. Layout triage now sends only chart/table/formula regions to the full VLM pipeline and uses RapidOCR for ordinary text. The same screenshot then completed in 21.203 seconds, with a 0.016-second cached repeat. DeepSeek remains the source identification and match-verification model; unverified matches cannot enter Processing.

With the weights already on disk, the optimized pipeline read the synthetic chart and table correctly in 178.844 seconds including model startup; the cached repeat took 0.015 seconds. Both visual evaluations made zero paid API calls.
