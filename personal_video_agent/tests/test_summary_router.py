import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from pva import intelligence, store, summary_router, tasks


class SummaryRouteTests(unittest.TestCase):
    def test_api_job_persists_route_and_keeps_credential_out_of_sqlite(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "test.db"
            db = store.connect(db_path)
            try:
                video_id = store.add_video(db, "https://example.org/video", title="Test")
                route = summary_router.SummaryRoute(
                    "api", "direct", "https://example.org/v1", "model", "test-secret")
                task_id = tasks.enqueue_processing_video(
                    db, video_id, mode="resummarize", summary_route=route)
                row = db.execute("SELECT * FROM processing_tasks WHERE id=?", (task_id,)).fetchone()
                self.assertNotIn("test-secret", row["summary_route_json"])
                saved = summary_router.SummaryRoute(**json.loads(row["summary_route_json"]))
                response = {"choices": [{"message": {"content": "{}"}}]}
                with summary_router.use(saved), patch(
                    "pva.summary_router._opener.open",
                    return_value=BytesIO(json.dumps(response).encode()),
                ) as fetch, patch("pva.intelligence._local_chat", return_value="local"):
                    self.assertEqual(intelligence.chat([], purpose="summary"), "{}")
                    request = fetch.call_args.args[0]
                    self.assertEqual(request.full_url, "https://example.org/v1/chat/completions")
                    self.assertEqual(request.get_header("Authorization"), "Bearer test-secret")
                    self.assertEqual(intelligence.chat([], purpose="classification"), "local")
                    fetch.assert_called_once()
                self.assertEqual(summary_router.current.get().backend, "local")
            finally:
                db.close()

    def test_batched_route_keeps_evidence_links_even_on_short_transcript(self):
        rows = [{"id": 1, "start": 0, "text": "Costs rose by 10%."}]
        replies = [
            '{"notes":[{"text":"Costs rose by 10%.","evidence":["s:1"]}]}',
            '{"takeaway":{"text":"Costs rose by 10%.","evidence":["s:1"]}}',
        ]
        with summary_router.use(summary_router.SummaryRoute(method="batched")), patch(
            "pva.intelligence.chat", side_effect=replies,
        ) as chat:
            summary, claims = intelligence.summarize(
                rows, [], target_language="en", skill_text="", feedback=[])
        self.assertEqual([call.kwargs["purpose"] for call in chat.call_args_list],
                         ["summary notes", "summary"])
        self.assertEqual(summary, "Costs rose by 10%.")
        self.assertEqual(claims[0]["evidence"], ["s:1"])
