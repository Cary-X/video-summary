import sqlite3
import tempfile
import unittest
from pathlib import Path

from pva import history, library, store, tasks, visual


class ProcessingHistoryTests(unittest.TestCase):
    def test_delete_keeps_outcomes_and_cleans_unshared_local_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = store.connect(root / "test.db")
            source = root / "uploads" / "upload.pdf"
            source.parent.mkdir()
            source.write_bytes(b"document")
            derived = source.with_name("upload-page-1.png")
            derived.write_bytes(b"image")
            external = root / "external.png"
            external.write_bytes(b"external")
            item = store.add_video(db, "pva-upload://first.pdf", title="Report.pdf", photo=str(external))
            other = store.add_video(db, "pva-upload://other.pdf", title="Shared report.pdf")
            with db:
                db.executemany("INSERT INTO uploaded_sources VALUES(?,?,?)",
                               [(key, str(source), "document") for key in (item, other)])
                db.execute("INSERT INTO visual_cache VALUES(?,?,?)",
                           (visual._image_cache_key(b"image", "cpu"), store.now(), "{}"))
            job = tasks.enqueue_processing_video(db, item)
            tasks._update(db, "processing_tasks", job, status="failed", stage="failed", progress=1)
            tasks.retry_processing(db, job)
            with self.assertRaisesRegex(ValueError, "active task"):
                history.remove_item(db, item, root, set())
            store.replace_segments(db, item, [{"start":0,"end":1,"text":"private source"}])
            store.save_result(db, item, "private summary", "finance", [])
            folder = library.create_collection(db, "Research")
            library.save_collections(db, item, [folder])
            tasks._update(db, "processing_tasks", job, status="ready", stage="complete", progress=1)
            with self.assertRaisesRegex(ValueError, "active task"):
                history.remove_item(db, item, root, {job})
            self.assertEqual(history.remove_item(db, item, root, set()), [])
            self.assertTrue(source.exists())
            self.assertTrue(derived.exists())
            self.assertTrue(external.exists())
            rows, count = history.page(db, "", 0, 10)
            self.assertEqual(count, 2)
            self.assertEqual({row["outcome"] for row in rows}, {"failed", "ready"})
            self.assertTrue(all(row["source"] == "Report.pdf" and row["item_status"] is None for row in rows))
            for table in ("videos", "segments", "collection_items", "processing_tasks"):
                field = "id" if table == "videos" else "video_id"
                self.assertIsNone(db.execute(f"SELECT 1 FROM {table} WHERE {field}=?", (item,)).fetchone())
            self.assertEqual(history.remove_item(db, other, root, set()), [])
            self.assertFalse(source.exists())
            self.assertFalse(derived.exists())
            self.assertIsNone(db.execute("SELECT 1 FROM visual_cache").fetchone())
            db.close()

    def test_backfills_failed_then_removed_legacy_task_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            db = store.connect(path)
            item = store.add_video(db, "https://youtu.be/example", title="Legacy video")
            job = tasks.enqueue_processing_video(db, item)
            with db:
                db.execute("DROP TRIGGER record_processing_outcome")
                db.execute("DROP TABLE processing_history")
                db.execute("DELETE FROM preferences WHERE key='processing_history_migrated'")
            tasks._update(db, "processing_tasks", job, status="failed", stage="failed", progress=1)
            tasks.skip_processing(db, job)
            db.close()
            for _ in range(2):
                store._initialized_paths.discard(str(path.resolve()))
                db = store.connect(path)
                rows, count = history.page(db, "", 0, 10)
                self.assertEqual(count, 2)
                self.assertEqual({row["outcome"] for row in rows}, {"failed", "skipped"})
                self.assertTrue(all(row["source"] == "https://youtu.be/example" for row in rows))
                db.close()

    def test_uploaded_document_keeps_table_and_image_evidence(self):
        from io import BytesIO
        from unittest.mock import patch
        from PIL import Image
        from docx import Document
        from pva import pipeline, summary_router, uploads

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = store.connect(root / "library.db")
            picture = BytesIO()
            Image.new("RGB", (300, 180), "white").save(picture, format="PNG")
            picture.seek(0)
            doc = Document()
            table = doc.add_table(rows=2, cols=2)
            table.cell(0, 0).text, table.cell(0, 1).text = "Year", "Revenue"
            table.cell(1, 0).text, table.cell(1, 1).text = "2025", "120"
            doc.add_picture(picture)
            source = BytesIO()
            doc.save(source)
            task = uploads.save(db, root, "Report.docx", source.getvalue(), "en", summary_router.SummaryRoute())
            video_id = db.execute("SELECT video_id FROM processing_tasks WHERE id=?", (task,)).fetchone()[0]
            with patch("pva.document_reader.image_text", return_value="Chart: Revenue rose."), patch(
                "pva.intelligence.summarize", return_value=("Revenue report.", [])
            ) as summarize, patch("pva.intelligence.classify", return_value="finance"):
                result = pipeline.process(db, video_id, asset_dir=root, skill_file=root / "unused.md")
            self.assertTrue(any("Year: 2025 | Revenue: 120" in row["text"] for row in result["segments"]))
            self.assertEqual(len(result["frames"]), 1)
            self.assertTrue(Path(result["frames"][0]["image_path"]).is_file())
            self.assertIn("Document and webpage summary", summarize.call_args.kwargs["skill_text"])
            db.close()

    def test_web_images_skip_logo_and_duplicate_content(self):
        from io import BytesIO
        from unittest.mock import MagicMock, patch
        from PIL import Image
        from pva import web_sources

        images = []
        web_sources.html_blocks(
            b'<main><p>Quarterly report</p><img src="/logo.png" alt="Logo">'
            b'<figure><img src="/chart.png"><figcaption>Quarterly revenue</figcaption></figure>'
            b'<img src="/duplicate.png"></main>', 'https://example.com/report', images=images)
        self.assertEqual([label for _, label in images], ["Quarterly revenue", ""])
        content = BytesIO()
        Image.new("RGB", (320, 200), "white").save(content, format="PNG")
        session = MagicMock()
        response = session.get.return_value.__enter__.return_value
        response.status_code = 200
        response.iter_content.side_effect = lambda _: iter([content.getvalue()])
        with tempfile.TemporaryDirectory() as tmp, patch("pva.web_sources.validate_url"), patch(
            "pva.document_reader.image_text", return_value="Revenue chart"
        ):
            retained = []
            blocks = web_sources._image_blocks(session, Path(tmp) / "report.html", images,
                float("inf"), None, lambda *image: retained.append(image))
            self.assertEqual(len(blocks), 1)
            self.assertIn("Quarterly revenue", blocks[0])
            self.assertTrue(retained[0][0].is_file())

    def test_reprocess_removed_url_and_delete_history_without_resurrection(self):
        from pva.summary_router import SummaryRoute

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = store.connect(root / "test.db")
            url = "https://youtu.be/example"
            video_id = store.add_video(db, url, title="Video")
            job = tasks.enqueue_processing_video(db, video_id)
            tasks._update(db, "processing_tasks", job, status="failed", stage="failed", progress=1)
            record = history.page(db, "", 0, 10)[0][0]
            history.remove_item(db, video_id, root, set())
            next_job = history.reprocess(db, record["id"], "zh-Hans",
                                         summary_route=SummaryRoute(method="batched"))
            row = db.execute("SELECT * FROM processing_tasks WHERE id=?", (next_job,)).fetchone()
            self.assertNotEqual(row["video_id"], video_id)
            self.assertIn('"method": "batched"', row["summary_route_json"])
            with self.assertRaisesRegex(ValueError, "already has a task"):
                history.reprocess(db, record["id"], "en", summary_route=SummaryRoute())
            with db:
                db.execute("DELETE FROM processing_history WHERE id=?", (record["id"],))
            with self.assertRaisesRegex(ValueError, "deleted"):
                history.reprocess(db, record["id"], "en", summary_route=SummaryRoute())
            db.close()
