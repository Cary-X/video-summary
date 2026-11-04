from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pva import intelligence, paddle_worker, pipeline, store, tasks, visual


class LocalVisualTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "test.db"
        self.db = store.connect(self.db_path)
        self.image = self.root / "chart.png"
        self.image.write_bytes(b"test chart contents")

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_layout_results_keep_chart_values_and_block_positions(self):
        blocks = paddle_worker.blocks_from_result({"res": {"parsing_res_list": [
            {"block_label": "chart", "block_content": "Month,Revenue\nJan,20\nFeb,30",
             "block_bbox": [1, 2, 100, 90], "block_order": 2},
            {"block_label": "image", "block_content": ""}]}})
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["label"], "chart")
        self.assertIn("Feb,30", blocks[0]["text"])
        self.assertEqual(blocks[0]["bbox"], [1, 2, 100, 90])

    def test_local_success_never_calls_paid_vision_api(self):
        with intelligence.usage_context(self.db_path), \
             patch("pva.visual.read", return_value={"model": visual.MODEL, "text": "Revenue: 30"}), \
             patch("pva.intelligence.chat", side_effect=AssertionError("paid API called")):
            text = intelligence.read_frame(self.image)
        self.assertIn("Revenue: 30", text)
        self.assertIn(visual.MODEL, text)

    def test_local_failure_is_visible_and_does_not_silently_spend_money(self):
        warnings = []
        with intelligence.usage_context(self.db_path), \
             patch("pva.visual.read", side_effect=RuntimeError("model timed out")), \
             patch("pva.intelligence.local_ocr", return_value="Revenue 30"), \
             patch("pva.intelligence.chat", side_effect=AssertionError("paid API called")):
            text = intelligence.read_frame(self.image, on_warning=warnings.append)
        self.assertIn("unverified", text)
        self.assertIn("Revenue 30", text)
        self.assertIn("model timed out", warnings[0])

    def test_paid_fallback_requires_preference(self):
        with self.db:
            self.db.execute("INSERT INTO preferences VALUES ('visual_api_fallback','true')")
        with intelligence.usage_context(self.db_path), \
             patch("pva.visual.read", side_effect=RuntimeError("local unavailable")), \
             patch("pva.intelligence.local_ocr", return_value=""), \
             patch("pva.intelligence.chat", return_value='{"text":"Revenue: 30"}') as api:
            text = intelligence.read_frame(self.image)
        self.assertIn("Revenue: 30", text)
        self.assertEqual(api.call_count, 1)

    def test_repeated_image_uses_cache_and_records_zero_api_calls(self):
        real_popen = subprocess.Popen
        result = {"model": visual.MODEL, "device": "cpu", "text": "Revenue: 30", "blocks": []}

        def fake_worker(command, **kwargs):
            output = command[5]
            script = "from pathlib import Path; Path(" + repr(output) + ").write_text(" + repr(json.dumps(result)) + ",encoding='utf-8')"
            return real_popen([sys.executable, "-c", script], **kwargs)

        with patch("pva.visual.python_path", return_value=Path(sys.executable)), \
             patch("pva.visual._start_worker", side_effect=fake_worker) as worker:
            first = visual.read(self.image, db_path=self.db_path)
            second = visual.read(self.image, db_path=self.db_path)
        self.assertEqual(first, second)
        self.assertEqual(worker.call_count, 1)
        usage = self.db.execute("SELECT cache_hit,status FROM local_visual_usage ORDER BY id").fetchall()
        self.assertEqual([x["cache_hit"] for x in usage], [0, 1])
        self.assertEqual(self.db.execute("SELECT count(*) FROM ai_usage").fetchone()[0], 0)

    def test_cancel_kills_local_worker_and_keeps_cleanup_safe_on_windows(self):
        real_popen = subprocess.Popen
        children = []

        def slow_worker(command, **kwargs):
            child = real_popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
            children.append(child)
            return child

        def cancel(detail):
            raise pipeline.ProcessingCancelled("User removed task")

        with patch("pva.visual.python_path", return_value=Path(sys.executable)), \
             patch("pva.visual._start_worker", side_effect=slow_worker):
            with self.assertRaises(pipeline.ProcessingCancelled):
                visual.read(self.image, db_path=self.db_path, on_progress=cancel)
        self.assertIsNotNone(children[0].poll())
        self.assertEqual(self.db.execute("SELECT status FROM local_visual_usage").fetchone()[0], "failed")

    def test_timeout_kills_worker_and_does_not_cache_failed_output(self):
        real_popen = subprocess.Popen
        children = []

        def slow_worker(command, **kwargs):
            child = real_popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
            children.append(child)
            return child

        with patch("pva.visual.python_path", return_value=Path(sys.executable)), \
             patch("pva.visual._start_worker", side_effect=slow_worker):
            with self.assertRaisesRegex(RuntimeError, "exceeded"):
                visual.read(self.image, db_path=self.db_path, timeout=0.01)
        self.assertIsNotNone(children[0].poll())
        self.assertEqual(self.db.execute("SELECT count(*) FROM visual_cache").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT status FROM local_visual_usage").fetchone()[0], "failed")

    def saved_video(self):
        video_id = store.add_video(self.db, "https://example.com/video", title="A chart")
        store.replace_segments(self.db, video_id, [{"start": 0, "end": 2, "text": "A chart."}])
        segment = store.detail(self.db, video_id)["segments"][0]
        with self.db:
            self.db.execute("INSERT INTO frames(video_id,segment_id,at_seconds,image_path,extracted_text) VALUES (?,?,?,?,?)",
                            (video_id, segment["id"], 1, str(self.image), "Original visual evidence"))
        store.save_result(self.db, video_id, "Original summary", "finance", [])
        return video_id

    def test_reread_saved_frames_needs_no_download_or_asr(self):
        video_id = self.saved_video()
        with patch("pva.intelligence.read_frame", return_value="New chart: 35"), \
             patch("pva.media.download_video", side_effect=AssertionError("download")), \
             patch("pva.media.transcribe", side_effect=AssertionError("ASR")), \
             patch("pva.pipeline.resummarize", return_value={}) as summary:
            pipeline.reread_visuals(self.db, video_id, skill_file=self.root / "SKILL.md")
        self.assertEqual(store.detail(self.db, video_id)["frames"][0]["extracted_text"], "New chart: 35")
        summary.assert_called_once()

    def test_failed_reread_preserves_previous_evidence_and_summary(self):
        video_id = self.saved_video()
        with patch("pva.intelligence.read_frame", side_effect=RuntimeError("local model failed")), \
             patch("pva.pipeline.resummarize", side_effect=AssertionError("paid summary")):
            result = pipeline.reread_visuals(self.db, video_id, skill_file=self.root / "SKILL.md")
        self.assertEqual(result["frames"][0]["extracted_text"], "Original visual evidence")
        self.assertEqual(result["summary"], "Original summary")

    def test_unverified_source_cannot_enter_processing(self):
        task_id = tasks.enqueue_find(self.db, "card", "photo.png", card={"title": "A video"})
        with self.db:
            self.db.execute("UPDATE finding_tasks SET status='ready',candidates_json=? WHERE id=?",
                (json.dumps([{"url": "https://example.com/video", "title": "Wrong video",
                              "verified": False}]), task_id))
        with self.assertRaisesRegex(ValueError, "not been verified"):
            tasks.enqueue_processing(self.db, task_id, 0, "en")
        self.assertEqual(self.db.execute("SELECT count(*) FROM processing_tasks").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM videos").fetchone()[0], 0)

    def test_photo_identification_keeps_deepseek_vision_and_does_not_use_paddle(self):
        with patch("pva.intelligence.local_ocr", return_value="A video"), \
             patch("pva.visual.read", side_effect=AssertionError("Paddle used for identity")), \
             patch("pva.intelligence.chat", return_value='{"videos":[{"title":"A video","author":"Creator"}]}') as api:
            result = intelligence.identify_video_photo(self.image)
        self.assertEqual(result["videos"][0]["title"], "A video")
        self.assertTrue(api.call_args.kwargs["vision"])
        self.assertEqual(api.call_args.kwargs["purpose"], "photo identification")


if __name__ == "__main__":
    unittest.main()
