from __future__ import annotations

import tempfile
import threading
import time
import unittest
import json
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from pva import intelligence, media, pipeline, presentation, store, tasks, usage_visuals


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = store.connect(self.root / "personal.db")
        self.skill = self.root / "SKILL.md"
        self.skill.write_text("Summarize only evidence.", encoding="utf-8")
        self.url = "https://www.youtube.com/watch?v=example"
        self.video_id = store.add_video(self.db, self.url, title="Example", target_language="en")

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_captions_skip_download_and_trace_summary(self):
        rows = [{"start": 1.0, "end": 3.0, "text": "The value increased."}]

        def summary(segments, frames, **kwargs):
            self.assertEqual(frames, [])
            return "The value increased.", [{"text": "The value increased.",
                                             "evidence": [f"s:{segments[0]['id']}"]}]

        with patch.object(media, "metadata", return_value={"url": self.url,
             "title": "Example", "author": "A", "published_at": "20261019",
             "duration": 10.0, "raw": {}}), \
             patch.object(media, "captions", return_value=(rows, "en")), \
             patch.object(media, "download_video", side_effect=AssertionError("download called")), \
             patch("pva.intelligence.summarize", side_effect=summary), \
             patch("pva.intelligence.classify", return_value="finance"):
            result = pipeline.process(self.db, self.video_id, asset_dir=self.root,
                                      skill_file=self.skill)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["category"], "finance")
        self.assertEqual(result["transcript_source"], "captions")
        self.assertEqual(result["claims"][0]["evidence"],
                         [f"s:{result['segments'][0]['id']}"])
        self.assertEqual(result["frames"], [])

    def test_refresh_uses_saved_evidence(self):
        with self.db:
            self.db.execute("UPDATE videos SET status='ready' WHERE id=?", (self.video_id,))
        store.replace_segments(self.db, self.video_id,
                               [{"start": 0.0, "end": 2.0, "text": "A fact."}])
        with patch.object(media, "metadata", side_effect=AssertionError("metadata called")), \
             patch("pva.intelligence.summarize", return_value=(
                 "A fact.", [{"text": "A fact.", "evidence": [
                     f"s:{store.detail(self.db, self.video_id)['segments'][0]['id']}"]}])), \
             patch("pva.intelligence.classify", return_value="other"):
            result = pipeline.process(self.db, self.video_id, asset_dir=self.root,
                                      skill_file=self.skill, refresh=True)
        self.assertEqual(result["status"], "ready")
        self.assertIn("A fact.", result["summary"])

    def test_vtt_parser_keeps_timestamps(self):
        content = "WEBVTT\n\n00:00:01.000 --> 00:00:03.500\nFirst line\n\n00:00:04.000 --> 00:00:05.000\nSecond line\n"
        self.assertEqual(media._parse_vtt(content), [
            {"start": 1.0, "end": 3.5, "text": "First line"},
            {"start": 4.0, "end": 5.0, "text": "Second line"},
        ])

    def test_caption_track_prefers_target_language(self):
        info = {"language": "en", "automatic_captions": {
            "ar-orig": [{"ext": "vtt", "url": "https://example.test/ar"}],
            "en": [{"ext": "vtt", "url": "https://example.test/en"}],
        }}
        vtt = b"WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nEnglish text\n"
        with patch("pva.media.urllib.request.urlopen", return_value=BytesIO(vtt)) as fetch:
            result = media.captions(info, preferred_language="en")
        self.assertEqual(result[1], "en")
        self.assertEqual(fetch.call_args.args[0].full_url, "https://example.test/en")

    def test_language_matches_region_but_not_script_or_cantonese(self):
        self.assertTrue(media.same_language("en-orig", "en"))
        self.assertTrue(media.same_language("zh-CN", "zh-Hans"))
        self.assertFalse(media.same_language("zh-Hans", "zh-Hant"))
        self.assertFalse(media.same_language("zh-Hant", "yue"))

    def test_sliding_captions_merge_without_repeating_words(self):
        rows = [
            {"start": 1, "end": 4, "text": "Google Flow has made adverts massively simpler to make."},
            {"start": 4, "end": 6, "text": "massively simpler to make. Every advert on your screen"},
            {"start": 6, "end": 9, "text": "Every advert on your screen was made with it."},
        ]
        merged = presentation.merge_transcript(rows)
        self.assertEqual(len(merged), 1)
        self.assertEqual((merged[0]["start"], merged[0]["end"]), (1, 9))
        self.assertEqual(merged[0]["text"].count("Every advert"), 1)

    def test_flash_request_records_provider_tokens(self):
        output = {"model": "deepseek-flash",
                  "choices": [{"message": {"content": "done"}}],
                  "usage": {"prompt_tokens": 120, "completion_tokens": 30,
                            "total_tokens": 150, "prompt_cache_hit_tokens": 20,
                            "prompt_cache_miss_tokens": 100}}
        with patch.dict("os.environ", {"PVA_LLM_BASE_URL": "https://api.deepseek.com",
                                    "PVA_LLM_API_KEY": "test-key",
                                    "PVA_LLM_MODEL": "deepseek-v4-pro"}), \
             patch("pva.intelligence.urllib.request.urlopen",
                   return_value=BytesIO(json.dumps(output).encode())) as fetch:
            with intelligence.usage_context(self.root / "personal.db",
                                            video_id=self.video_id):
                self.assertEqual(intelligence.chat([{"role": "user", "content": "Hi"}],
                                                   purpose="summary"), "done")
        body = json.loads(fetch.call_args.args[0].data)
        self.assertEqual(body["model"], "deepseek-flash")
        row = self.db.execute("SELECT * FROM ai_usage").fetchone()
        self.assertEqual((row["purpose"], row["total_tokens"], row["response_model"]),
                         ("summary", 150, "deepseek-flash"))
        self.assertGreater(row["estimated_cost_low_usd"], 0)

    def test_usage_logging_can_write_while_video_list_is_open(self):
        listing = self.db.execute("SELECT id FROM videos")
        first = listing.fetchone()
        other = store.connect(self.root / "personal.db")
        try:
            store.record_ai_usage(
                other, video_id=first["id"], finding_task_id=None,
                purpose="summary", requested_model="deepseek-flash",
                response_model="deepseek-flash",
                usage={"prompt_tokens": 2, "completion_tokens": 3,
                       "total_tokens": 5})
            self.assertEqual(self.db.execute("SELECT count(*) FROM ai_usage").fetchone()[0], 1)
        finally:
            listing.close()
            other.close()

    def test_usage_charts_are_thin_and_zero_based(self):
        prepared = usage_visuals.prepare_usage([{
            "at": "2026-10-20T01:00:00+00:00", "prompt_tokens": -10,
            "completion_tokens": 5, "total_tokens": -5,
            "cache_hit_tokens": 0, "cache_miss_tokens": 0,
            "estimated_cost_low_usd": -1, "estimated_cost_high_usd": 0.1,
        }])
        self.assertEqual(prepared.iloc[0]["prompt_tokens"], 0)
        self.assertEqual(prepared.iloc[0]["estimated_cost_low_usd"], 0)
        daily = usage_visuals.complete_days(
            prepared, ["prompt_tokens", "completion_tokens"], "7 days")
        spec = usage_visuals.daily_bars(
            daily, {"prompt_tokens": "Input", "completion_tokens": "Output"},
            title="Tokens", colors=["#111111", "#222222"]).to_dict()
        self.assertEqual(spec["mark"]["size"], 5)
        self.assertEqual(spec["encoding"]["y"]["scale"]["domain"][0], 0)
        breakdown = usage_visuals.category_bars(
            prepared.assign(label="Flash"), "label", "total_tokens",
            title="By model").to_dict()
        self.assertEqual(breakdown["mark"]["size"], 10)
        self.assertEqual(breakdown["encoding"]["x"]["scale"]["domain"][0], 0)

    def test_usage_is_split_by_api_step_with_local_ocr_at_zero(self):
        rows = []
        for purpose, tokens, cost in (("photo identification", 10, 0.01),
                                      ("match checking", 20, 0.02),
                                      ("summary notes", 30, 0.03),
                                      ("summary", 40, 0.04)):
            rows.append({"at": "2026-10-20T01:00:00+00:00", "purpose": purpose,
                         "prompt_tokens": tokens - 2, "completion_tokens": 2,
                         "total_tokens": tokens, "cache_hit_tokens": 0,
                         "cache_miss_tokens": tokens - 2,
                         "estimated_cost_low_usd": cost / 2,
                         "estimated_cost_high_usd": cost})
        totals = usage_visuals.step_totals(usage_visuals.prepare_usage(rows))
        by_step = totals.set_index("task_step")
        self.assertEqual(by_step.loc["Identify photo", "tokens"], 10)
        self.assertEqual(by_step.loc["Match video", "cost_high_usd"], 0.02)
        self.assertEqual(by_step.loc["Summarize · final brief", "tokens"], 40)
        self.assertEqual(by_step.loc["Local OCR", "tokens"], 0)
        self.assertEqual(by_step.loc["Local ASR", "cost_high_usd"], 0)

    def test_bot_challenge_retries_with_selected_browser_cookies(self):
        anonymous = MagicMock()
        anonymous.__enter__.return_value.extract_info.side_effect = RuntimeError(
            "Sign in to confirm you're not a bot")
        signed_in = MagicMock()
        signed_in.__enter__.return_value.extract_info.return_value = {
            "webpage_url": self.url, "title": "Example", "uploader": "A"}
        with patch("pva.media._ydl", side_effect=[anonymous, signed_in]) as ydl:
            result = media.metadata(self.url, cookie_browser="chrome")
        self.assertEqual(result["title"], "Example")
        self.assertEqual(ydl.call_args.kwargs["cookie_browser"], "chrome")

    def test_chrome_cookie_failure_falls_back_to_firefox(self):
        anonymous = MagicMock()
        anonymous.__enter__.return_value.extract_info.side_effect = RuntimeError(
            "Sign in to confirm you're not a bot")
        chrome = MagicMock()
        chrome.__enter__.return_value.extract_info.side_effect = RuntimeError(
            "failed to load cookies")
        firefox = MagicMock()
        firefox.__enter__.return_value.extract_info.return_value = {
            "webpage_url": self.url, "title": "Example", "uploader": "A"}
        with patch("pva.media._ydl", side_effect=[anonymous, chrome, firefox]) as ydl:
            self.assertEqual(media.metadata(self.url, cookie_browser="chrome")["title"],
                             "Example")
        self.assertEqual([call.kwargs["cookie_browser"] for call in ydl.call_args_list],
                         ["", "chrome", "firefox"])

    def test_auto_browser_tries_firefox_first(self):
        self.assertEqual(media._cookie_browsers("auto"),
                         ["firefox", "chrome", "edge"])

    def test_chart_reference_keeps_frame_and_removes_temp_media(self):
        rows = [{"start": 1.0, "end": 3.0, "text": "The chart shows growth."}]

        def download(url, directory, **kwargs):
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "source.mp4"
            path.write_bytes(b"test video")
            return path

        def capture(video, at, output):
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"test frame")
            return output

        with patch.object(media, "metadata", return_value={"url": self.url,
             "title": "Example", "author": "A", "published_at": "20261019",
             "duration": 10.0, "raw": {}}), \
             patch.object(media, "captions", return_value=(rows, "en")), \
             patch.object(media, "download_video", side_effect=download) as fetch, \
             patch.object(media, "capture_frame", side_effect=capture), \
             patch("pva.intelligence.read_frame", return_value="Revenue: 20"), \
             patch("pva.intelligence.summarize", return_value=(
                 "Revenue grew.", [{"text": "Revenue grew.", "evidence": []}])), \
             patch("pva.intelligence.classify", return_value="finance"):
            result = pipeline.process(self.db, self.video_id, asset_dir=self.root,
                                      skill_file=self.skill)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(len(result["frames"]), 1)
        self.assertEqual(result["frames"][0]["extracted_text"], "Revenue: 20")
        self.assertTrue(Path(result["frames"][0]["image_path"]).is_file())
        self.assertFalse(list(self.root.glob("ingest-*")))

    def test_visual_download_block_does_not_discard_transcript(self):
        rows = [{"start": 1.0, "end": 3.0, "text": "The chart shows growth."}]
        with patch.object(media, "metadata", return_value={"url": self.url,
             "title": "Example", "author": "A", "published_at": "20261019",
             "duration": 10.0, "raw": {}}), \
             patch.object(media, "captions", return_value=(rows, "en")), \
             patch.object(media, "download_video", side_effect=RuntimeError("not a bot")), \
             patch("pva.intelligence.summarize", return_value=(
                 "Growth.", [{"text": "Growth.", "evidence": []}])), \
             patch("pva.intelligence.classify", return_value="finance"):
            result = pipeline.process(self.db, self.video_id, asset_dir=self.root,
                                      skill_file=self.skill)
        self.assertEqual(result["status"], "ready")
        self.assertTrue(any(event["stage"] == "visual warning" for event in result["events"]))

    def test_visual_retry_uses_stored_transcript_and_refreshes_summary(self):
        with self.db:
            self.db.execute("UPDATE videos SET status='ready',summary='Old' WHERE id=?",
                            (self.video_id,))
        store.replace_segments(self.db, self.video_id, [
            {"start": 1.0, "end": 3.0, "text": "The chart shows growth."}])

        def download(url, directory, **kwargs):
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "source.mp4"
            path.write_bytes(b"video")
            self.assertEqual(kwargs["purpose"], "visual")
            return path

        def frame(video, at, output):
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"frame")
            return output

        with patch.object(media, "metadata", side_effect=AssertionError("metadata called")), \
             patch.object(media, "download_video", side_effect=download), \
             patch.object(media, "capture_frame", side_effect=frame), \
             patch("pva.intelligence.read_frame", return_value="Revenue +20%"), \
             patch("pva.intelligence.summarize", return_value=(
                 "Growth is visible.", [{"text": "Growth is visible.", "evidence": []}])), \
             patch("pva.intelligence.classify", return_value="finance"):
            result = pipeline.repair_visuals(self.db, self.video_id,
                                             asset_dir=self.root, skill_file=self.skill)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["frames"][0]["extracted_text"], "Revenue +20%")
        self.assertEqual(result["summary"], "Growth is visible.")

    def test_visual_cues_spread_across_video(self):
        rows = [{"start": second, "end": second + 2,
                 "text": "This chart shows the result."}
                for second in (1, 2, 3, 60, 120, 180)]
        selected = pipeline._visual_cues(rows, max_frames=4)
        self.assertEqual([row["start"] for row in selected], [1, 60, 120, 180])

    def test_no_captions_uses_asr(self):
        with patch.object(media, "metadata", return_value={"url": self.url,
             "title": "Example", "author": "A", "published_at": "20261019",
             "duration": 10.0, "raw": {}}), \
             patch.object(media, "captions", return_value=None), \
             patch.object(media, "download_video", return_value=self.root / "video.mp4"), \
             patch.object(media, "transcribe", return_value=(
                 [{"start": 0.0, "end": 2.0, "text": "A fact."}], "en")), \
             patch("pva.intelligence.summarize", return_value=(
                 "A fact.", [{"text": "A fact.", "evidence": []}])), \
             patch("pva.intelligence.classify", return_value="other"):
            result = pipeline.process(self.db, self.video_id, asset_dir=self.root,
                                      skill_file=self.skill)
        self.assertEqual(result["transcript_source"], "asr")

    def test_feedback_and_measurement(self):
        pipeline.feedback(self.db, self.video_id, 5, "More detail on figures")
        self.assertEqual(pipeline.measures(self.db)["average_rating"], 5.0)
        self.assertEqual(store.detail(self.db, self.video_id)["feedback"][0]["note"],
                         "More detail on figures")

    def test_summary_keeps_only_valid_evidence(self):
        segments = [{"id": 7, "start": 1.0, "text": "Revenue rose"}]
        responses = [
            '{"notes":[{"text":"Revenue rose","evidence":["s:7"]}]}',
            '{"summary":"Unsupported overview","claims":[{"text":"Revenue rose","evidence":["s:7"]},{"text":"Invented","evidence":["s:999"]}]}',
        ]
        with patch("pva.intelligence.chat", side_effect=responses):
            summary, claims = intelligence.summarize(
                segments, [], target_language="en", skill_text="Ground claims", feedback=[])
        self.assertEqual(summary, "**Key points**\n- Revenue rose")
        self.assertEqual(len(claims), 1)

    def test_professional_summary_sections_keep_evidence_out_of_text(self):
        segments = [{"id": 7, "start": 1.0, "text": "The new tool halves ad production time."}]
        responses = [
            '{"notes":[{"text":"Production time halves","evidence":["s:7"]}]}',
            '{"takeaway":{"text":"Ad production becomes faster.","evidence":["s:7"]},'
            '"importance":{"text":"Teams can make more variants.","evidence":["s:7"]},'
            '"key_points":[{"text":"Production time falls by half.","evidence":["s:7"]}],'
            '"caveats":[]}',
        ]
        with patch("pva.intelligence.chat", side_effect=responses):
            summary, claims = intelligence.summarize(
                segments, [], target_language="en", skill_text="Brief the reader", feedback=[])
        self.assertIn("**Why it matters**", summary)
        self.assertIn("Production time falls by half", summary)
        self.assertNotIn("s:7", summary)
        self.assertEqual(claims[0]["evidence"], ["s:7"])

    def test_summary_does_not_show_numbers_in_code_font(self):
        raw = "**Why it matters**\nFrom `639 a year to `0."
        self.assertEqual(presentation.clean_summary(raw),
                         "**Why it matters**\nFrom 639 a year to 0.")

    def test_skipping_one_card_keeps_other_video_available(self):
        left = tasks.enqueue_find(self.db, "card", "photo.jpg",
                                  card={"title": "Left"})
        right = tasks.enqueue_find(self.db, "card", "photo.jpg",
                                   card={"title": "Right"})
        with self.db:
            for task_id in (left, right):
                self.db.execute("UPDATE finding_tasks SET status='ready' WHERE id=?",
                                (task_id,))
        tasks.skip_finding(self.db, left)
        statuses = {row["id"]: row["status"] for row in self.db.execute(
            "SELECT id,status FROM finding_tasks WHERE id IN (?,?)", (left, right))}
        self.assertEqual(statuses, {left: "skipped", right: "ready"})
        with self.assertRaises(ValueError):
            tasks.enqueue_processing(self.db, left, 0, "en")

    def test_skipping_photo_before_split_skips_its_cards(self):
        parent = tasks.enqueue_find(self.db, "photo", "photo.jpg")
        child = tasks.enqueue_find(self.db, "card", "photo.jpg",
                                   parent_id=parent, card={"title": "Child"})
        tasks.skip_finding(self.db, parent)
        statuses = {row["id"]: row["status"] for row in self.db.execute(
            "SELECT id,status FROM finding_tasks WHERE id IN (?,?)", (parent, child))}
        self.assertEqual(statuses, {parent: "skipped", child: "skipped"})

    def test_skipping_running_finding_does_not_requeue_video(self):
        task_id = tasks.enqueue_find(self.db, "link", self.url)
        self.db.close()
        manager = tasks.TaskManager(self.root / "personal.db", self.root, self.skill)
        started, release = threading.Event(), threading.Event()

        def metadata(url, **kwargs):
            started.set()
            self.assertTrue(release.wait(5))
            return {"url": url, "title": "Example", "author": "A",
                    "published_at": "", "duration": 10}

        try:
            with patch("pva.tasks.media.metadata", side_effect=metadata):
                manager.pump()
                self.assertTrue(started.wait(5))
                db = store.connect(self.root / "personal.db")
                tasks.skip_finding(db, task_id)
                db.close()
                release.set()
                manager.find_pool.shutdown(wait=True)
            db = store.connect(self.root / "personal.db")
            self.assertEqual(db.execute(
                "SELECT status FROM finding_tasks WHERE id=?", (task_id,)
                ).fetchone()["status"], "skipped")
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM processing_tasks").fetchone()[0], 0)
            db.close()
        finally:
            release.set()
            manager.find_pool.shutdown(wait=True)
            manager.process_pool.shutdown(wait=True)
            self.db = store.connect(self.root / "personal.db")

    def test_one_photo_can_identify_two_video_cards(self):
        image = self.root / "photo.png"
        image.write_bytes(b"test image")
        with patch("pva.intelligence.local_ocr", return_value="My Video\nAlice\n2026-10-19 12:30"), \
             patch("pva.intelligence.chat", return_value=(
                 '{"videos":[{"title":"Left Video","author":"Alice","published_at":"",'
                 '"duration":"5:30","position":"left","bbox":[0,100,480,600]},'
                 '{"title":"Right Video","author":"Bob","published_at":"",'
                 '"duration":"14:39","position":"right","bbox":[500,100,1000,600]}]}')) as model:
            result = intelligence.identify_video_photo(image)
        self.assertEqual([x["title"] for x in result["videos"]],
                         ["Left Video", "Right Video"])
        self.assertEqual([x["author"] for x in result["videos"]], ["Alice", "Bob"])
        self.assertEqual(len(result["videos"]), 2)
        self.assertTrue(model.call_args.kwargs["vision"])
        self.assertEqual(model.call_args.args[0][-1]["content"][1]["type"], "image_url")

    def test_model_marks_unrelated_search_result_unverified(self):
        video = {"title": "Left Video", "author": "Alice", "duration": "5:30"}
        candidates = [
            {"url": "https://youtube.com/watch?v=1", "title": "Left Video", "author": "Alice", "score": 1.0},
            {"url": "https://youtube.com/watch?v=2", "title": "Other Video", "author": "Bob", "score": 0.1},
        ]
        with patch("pva.intelligence.chat", return_value=(
                '{"matches":[{"index":0,"match":true,"confidence":0.97,"reason":"same title and creator"},'
                '{"index":1,"match":false,"confidence":0.02,"reason":"different video"}]}')):
            result = intelligence.verify_video_matches(video, candidates)
        self.assertTrue(result[0]["verified"])
        self.assertFalse(result[1]["verified"])

    def test_failed_processing_task_does_not_stop_next(self):
        second = store.add_video(self.db, self.url + "2", title="Second")
        first_task = tasks.enqueue_processing_video(self.db, self.video_id)
        second_task = tasks.enqueue_processing_video(self.db, second)
        self.db.close()
        manager = tasks.TaskManager(self.root / "personal.db", self.root, self.skill)

        def work(db, video_id, **kwargs):
            if video_id == self.video_id:
                raise RuntimeError("source blocked")
            return {}

        try:
            with patch("pva.tasks.pipeline.process", side_effect=work):
                manager.pump()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    check = store.connect(self.root / "personal.db")
                    statuses = [r["status"] for r in check.execute(
                        "SELECT status FROM processing_tasks ORDER BY created_at,id")]
                    check.close()
                    if set(statuses) == {"failed", "ready"}:
                        break
                    time.sleep(0.05)
                self.assertEqual(set(statuses), {"failed", "ready"})
        finally:
            manager.find_pool.shutdown(wait=True)
            manager.process_pool.shutdown(wait=True)
            self.db = store.connect(self.root / "personal.db")

    def test_photo_finding_splits_into_independent_video_tasks(self):
        photo = self.root / "photo.jpg"
        photo.write_bytes(b"photo")
        parent = tasks.enqueue_find(self.db, "photo", str(photo), photo_path=str(photo))
        self.db.close()
        manager = tasks.TaskManager(self.root / "personal.db", self.root, self.skill)
        cards = [{"title": "Left", "author": "A"},
                 {"title": "Right", "author": "B"}]

        def search(title, *args, **kwargs):
            return [{"url": f"https://youtube.com/watch?v={title}",
                     "title": title, "author": "A", "score": 1.0}]

        def verify(card, candidates):
            return [{**candidates[0], "verified": True, "confidence": 1.0}]

        try:
            with patch("pva.tasks.intelligence.identify_video_photo",
                       return_value={"videos": cards}), \
                 patch("pva.tasks.media.search_candidates", side_effect=search), \
                 patch("pva.tasks.intelligence.verify_video_matches", side_effect=verify):
                manager.pump()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    check = store.connect(self.root / "personal.db")
                    rows = [dict(r) for r in check.execute(
                        "SELECT kind,status,card_json,parent_id FROM finding_tasks")]
                    check.close()
                    if len(rows) == 3 and sum(r["status"] == "ready" for r in rows) == 2:
                        break
                    time.sleep(0.05)
                self.assertEqual(len(rows), 3)
                self.assertEqual({r["status"] for r in rows if r["kind"] == "photo"},
                                 {"split"})
                self.assertEqual({r["status"] for r in rows if r["kind"] == "card"},
                                 {"ready"})
                self.assertEqual({r["parent_id"] for r in rows if r["kind"] == "card"},
                                 {parent})
                self.assertEqual({r["kind"] for r in rows if r["kind"] == "card"},
                                 {"card"})
        finally:
            manager.find_pool.shutdown(wait=True)
            manager.process_pool.shutdown(wait=True)
            self.db = store.connect(self.root / "personal.db")


if __name__ == "__main__":
    unittest.main()
