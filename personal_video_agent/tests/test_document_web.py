import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from pva import uploads, web_sources, pipeline, store, summary_router
from pva.document_reader import pdf_blocks

class SourceTests(unittest.TestCase):
    def test_html_article_and_table(self):
        title, blocks = web_sources.html_blocks(b'<html><title>Report</title><nav>BAD MENU</nav><main><p>Revenue grew during the year.</p><table><tr><th>Year</th><th>Revenue</th></tr><tr><td>2025</td><td>$123</td></tr></table></main></html>', 'https://example.com/report')
        self.assertEqual(title, 'Report')
        text='\n'.join(blocks)
        self.assertIn('Year: 2025 | Revenue: $123',text)
        self.assertNotIn('BAD MENU',text)

    def test_private_urls_rejected(self):
        for url in ['http://127.0.0.1/admin','http://[::1]/','file:///etc/passwd','https://user:pass@example.com']:
            with self.assertRaises(ValueError):
                web_sources.validate_url(url)

    def test_pdf_scan_renders_and_calls_ocr(self):
        from PIL import Image, ImageDraw
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'scan.pdf'
            image=Image.new('RGB',(800,600),'white')
            ImageDraw.Draw(image).text((40,40),'Year  Revenue\n2025  $123',fill='black')
            image.save(path,'PDF')
            with patch('pva.document_reader.image_text',return_value='Year: 2025 | Revenue: $123') as ocr:
                blocks=pdf_blocks(path)
                self.assertIn('Page 1 (visual evidence)',blocks[0])
                self.assertIn('$123',blocks[0])
                self.assertTrue(ocr.call_args.args[0].is_file())

    def test_pdf_digital_table_cells(self):
        from pypdf import PdfWriter
        from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'table.pdf'
            writer=PdfWriter()
            page=writer.add_blank_page(width=400,height=300)
            font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
            page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
            stream=DecodedStreamObject()
            stream.set_data(b'0 0 0 RG 40 160 260 80 re S 40 200 m 300 200 l S 170 160 m 170 240 l S BT /F1 12 Tf 50 215 Td (Year) Tj 130 0 Td (Revenue) Tj -130 -40 Td (2025) Tj 130 0 Td ($123) Tj ET')
            page[NameObject('/Contents')]=writer._add_object(stream)
            writer.write(path)
            with patch('pva.document_reader.image_text',side_effect=AssertionError('Unexpected OCR')):
                text='\n'.join(pdf_blocks(path))
            self.assertIn('Year: 2025 | Revenue: $123',text)

    def test_webpage_routes_as_document_and_saves_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); db=store.connect(root/'test.db')
            route=summary_router.SummaryRoute(backend='llama.cpp')
            with patch('pva.web_sources.validate_url',side_effect=lambda value:value):
                task=web_sources.enqueue(db,'https://example.com/report','en',route)
            video=db.execute('SELECT video_id FROM processing_tasks WHERE id=?',(task,)).fetchone()[0]
            skill=root/'skill.md';skill.write_text('Grounded summary.')
            original=root/'page.html';original.write_text('<main>Revenue grew</main>')
            with patch('pva.web_sources.extract',return_value=(original,'Report',[{'start':0,'end':1,'text':'Revenue grew.'}])), patch('pva.intelligence.summarize',return_value=('Revenue grew.',[])), patch('pva.intelligence.classify',return_value='finance'), patch('pva.media.metadata',side_effect=AssertionError('Video lookup')):
                result=pipeline.process(db,video,asset_dir=root,skill_file=skill)
            self.assertEqual(result['title'],'Report')
            self.assertEqual(result['status'],'ready')
            self.assertEqual(result['transcript_source'],'document')
            db.close()

    def test_merged_html_cells_preserve_column_alignment(self):
        _, blocks=web_sources.html_blocks('<main><table><tr><th>Region</th><th>Year</th><th>Revenue</th></tr><tr><td rowspan="2">Asia</td><td>2024</td><td>100</td></tr><tr><td>2025</td><td>200</td></tr></table></main>','https://example.com')
        self.assertIn('Region: Asia | Year: 2025 | Revenue: 200','\n'.join(blocks))

    def test_redirect_to_private_host_is_not_followed(self):
        from unittest.mock import MagicMock
        session=MagicMock()
        response=session.get.return_value.__enter__.return_value
        response.status_code=302
        response.headers={'Location':'http://127.0.0.1/private'}
        def addresses(host,*args):
            ip='127.0.0.1' if host=='127.0.0.1' else '93.184.216.34'
            return [(2,1,6,'',(ip,443))]
        with tempfile.TemporaryDirectory() as tmp, patch('requests.Session') as factory, patch('socket.getaddrinfo',side_effect=addresses):
            factory.return_value.__enter__.return_value=session
            with self.assertRaisesRegex(ValueError,'public'):
                web_sources.extract('https://example.com',Path(tmp))
            self.assertEqual(session.get.call_count,1)
