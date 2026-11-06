import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from pva import pipeline, presentation, store, summary_router, uploads

class UploadTests(unittest.TestCase):
    def test_audio_and_documents_skip_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = store.connect(root / "test.db")
            skill = root / "skill.md"
            skill.write_text("Grounded summary.", encoding="utf-8")
            route = summary_router.SummaryRoute(backend="llama.cpp")
            for name, content in [("notes.txt", b"Revenue rose. See chart."), ("audio.wav", b"mock audio")]:
                task_id = uploads.save(db, root, name, content, "en", route)
                self.assertEqual(uploads.save(db, root, name, content, "en", route), task_id)
                video_id = db.execute("SELECT video_id FROM processing_tasks WHERE id=?", (task_id,)).fetchone()[0]
                with patch("pva.media.metadata", side_effect=AssertionError("Network")), patch(
                    "pva.media.download_video", side_effect=AssertionError("Download")
                ), patch("pva.media.transcribe", return_value=([{"start":0,"end":3,"text":"Source."}],"en")
                ) as asr, patch("pva.intelligence.translate", side_effect=AssertionError("Translation")
                ), patch("pva.intelligence.summarize", return_value=("Summary.", [])
                ), patch("pva.intelligence.classify", return_value="tech"):
                    result = pipeline.process(db, video_id, asset_dir=root, skill_file=skill)
                    self.assertEqual(result["status"], "ready")
                    self.assertEqual(asr.call_count, int(name.endswith(".wav")))
            db.close()

    def test_docx_tables_encoding_and_scan_pdf(self):
        from docx import Document
        from pypdf import PdfWriter
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            doc = Document()
            doc.add_paragraph("Results")
            row = doc.add_table(rows=1, cols=2).rows[0]
            row.cells[0].text, row.cells[1].text = "Revenue", "$100"
            doc.save(root / "test.docx")
            self.assertIn("Revenue | $100", " ".join(x["text"] for x in uploads.document_segments(root / "test.docx")))
            (root / "test.txt").write_text("中文文本", encoding="utf-16")
            self.assertEqual(uploads.document_segments(root / "test.txt")[0]["text"], "中文文本")
            writer = PdfWriter()
            writer.add_blank_page(width=100, height=100)
            writer.write(root / "scan.pdf")
            with self.assertRaisesRegex(ValueError, "OCR"):
                uploads.document_segments(root / "scan.pdf")

    def test_document_sources_have_no_fake_timestamps(self):
        html = presentation.summary_html({"transcript_source":"document","summary":"Revenue rose.",
            "segments":[{"id":1,"start":0,"end":1,"text":"Revenue rose."}],
            "claims":[{"text":"Revenue rose.","evidence":["s:1"]}]})
        self.assertIn("Document text", html)
        self.assertNotIn("00:00:", html)
