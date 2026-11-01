import base64
import io
import json
import dataclasses
import subprocess
import sys
from unittest.mock import patch

from PIL import Image
from fastapi.testclient import TestClient

from app import db, pdfs
from app.main import app
from test_codex_workspace import course, build
from test_pdfs import VID, sample_pdf


def upload():
    db.execute('INSERT INTO videos VALUES (?,?,?,?,?,?,?)', (VID, 'PDF', 'lesson.mp4', 1, db.now_iso(), 'done', None))
    return TestClient(app).post(f'/api/videos/{VID}/pdfs', params={'name': 'lecture.pdf'}, content=sample_pdf()).json()


def test_pdf_image_evidence_and_search(course):
    item = upload()
    ws = build(course)
    args = {'id': item['id'], 'revision': item['revision']}
    search = ws.call('search_pdf', {**args, 'query': 'gradient'})
    assert search['success'], search
    assert json.loads(search['contentItems'][0]['text'])['results'][0]['page'] == 1
    assert not ws.pdf_read_ids
    result = ws.call('view_pdf_page', {**args, 'page': 1})
    assert result['success'], result
    image = Image.open(io.BytesIO(base64.b64decode(result['contentItems'][1]['imageUrl'].split(',')[1])))
    assert image.size == (1200, 1600)
    assert image.convert('L').getextrema()[0] < 100  # real text was rendered
    with patch('app.pdfs.subprocess.run', side_effect=AssertionError('cache missed')):
        assert ws.call('view_pdf_page', {**args, 'page': 1})['success']
    cropped = ws.call('view_pdf_page', {**args, 'page': 1, 'crop': [0, 0, 0.5, 0.25]})
    assert cropped['success']
    crop_image = Image.open(io.BytesIO(base64.b64decode(cropped['contentItems'][1]['imageUrl'].split(',')[1])))
    assert crop_image.size == (1600, 1067)
    assert crop_image.convert('L').getextrema()[0] < 100
    assert ws.log['pdf_views'][-1]['crop'] == [0, 0, 0.5, 0.25]
    ws.options['max_images'] = ws.images
    cite = f"PDF:{item['id']}:{item['revision']}:2"
    assert cite not in ws.pdf_read_ids
    assert ws.call('view_pdf_page', {**args, 'page': 2})['success']
    assert cite in ws.pdf_read_ids and cite in build(course).pdf_read_ids
    assert not build(course, 'other').pdf_read_ids
    assert pdfs.resolve(f'[{cite}]', VID, ws.pdf_read_ids)[0]
    pdfs.delete(VID, item['id'])
    assert not ws.call('view_pdf_page', {**args, 'page': 1})['success']
    assert not pdfs.resolve(f'[{cite}]', VID, ws.pdf_read_ids)[0]


def test_pdf_image_parameter_and_budget_boundaries(course):
    item = upload()
    ws = build(course)
    args = {'id': item['id'], 'revision': item['revision'], 'page': 1}
    for bad in ({'page': True}, {'page': 0}, {'page': 3}, {'revision': None},
                {'crop': [0, 0, 0, 1]}, {'crop': [-1, 0, 1, 1]},
                {'crop': [0, 0, float('nan'), 1]}, {'crop': [False, 0, 1, 1]}):
        assert not ws.call('view_pdf_page', {**args, **bad})['success']
    ws.options['max_read_tokens'] = 1
    assert ws.call('view_pdf_page', args)['success']
    assert ws.pdf_read_ids
    ws.options['max_tool_calls'] = ws.calls
    assert ws.call('search_pdf', {'id': item['id'], 'revision': item['revision'], 'query': 'gradient'})['success']


def test_pdf_worker_failure_limits_and_cache_version(course, set_settings):
    from app.qa.notes import NoteError
    import pytest
    item = upload()
    args = (VID, item['id'], item['revision'])
    with patch('app.pdfs.subprocess.run', side_effect=subprocess.TimeoutExpired('worker', 1)):
        with pytest.raises(NoteError, match='超时'):
            pdfs.view_page(*args, 1)
    assert not list(pdfs.folder(VID, item['id']).glob('.read-*'))
    page = pdfs.view_page(*args, 1)
    assert page['width'] == 1200
    set_settings(dataclasses.replace(pdfs.settings, pdfs=dataclasses.replace(pdfs.settings.pdfs, image_long_edge=800)))
    assert pdfs.view_page(*args, 1)['width'] == 600  # settings invalidate cached output
    set_settings(dataclasses.replace(pdfs.settings, pdfs=dataclasses.replace(pdfs.settings.pdfs, max_image_bytes=1)))
    with pytest.raises(NoteError, match='大小上限'):
        pdfs.view_page(*args, 1)


def test_worker_network_is_blocked():
    code = ('import sys,socket; from app.pdf_read_worker import deny_network; '
            'sys.addaudithook(deny_network); socket.create_connection(("127.0.0.1",9))')
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, encoding='utf-8', errors='replace')
    assert result.returncode != 0 and 'PermissionError' in result.stderr


def test_rotated_pdf_crop_coordinates(tmp_path):
    from pypdf import PdfReader, PdfWriter
    from app.pdf_read_worker import render
    writer = PdfWriter()
    writer.add_page(PdfReader(io.BytesIO(sample_pdf())).pages[0]).rotate(90)
    source = tmp_path / 'rotated.pdf'
    writer.write(source)
    limits = dataclasses.asdict(pdfs.settings.pdfs)
    whole = render(source, 1, None, limits, 1600)
    assert whole.size == (1600, 1200)
    # Original top-left text rotates into the top-right quadrant.
    right = render(source, 1, [0.75, 0, 1, 0.5], limits, 1600)
    left = render(source, 1, [0, 0, 0.25, 0.5], limits, 1600)
    assert right.convert('L').getextrema()[0] < 100
    assert left.convert('L').getextrema() == (255, 255)


def test_ocr_cache_search_and_native_fallback(course):
    from app import artifacts
    item = upload()
    args = (VID, item['id'], item['revision'])
    directory = pdfs.folder(VID, item['id'])
    cached = pdfs._cache_path(directory, item['revision'], {'kind': 'ocr', 'page': 2})
    artifacts.write_json(cached, {'text': '扫描图中的梯度 descent', 'source': 'docling', 'has_text': True})
    with patch('app.pdfs.subprocess.run', side_effect=AssertionError('must use cached OCR')):
        rows = pdfs.read_pages(*args, 2, 2)
        assert rows[0]['source'] == 'docling'
        found = pdfs.search_pages(*args, '梯度')
        assert found['results'][0]['page'] == 2 and found['coverage']['ocr_pages'] == [2]
    with patch('app.pdfs._page_job', side_effect=pdfs.notes.NoteError('OCR unavailable')):
        row = pdfs.read_pages(*args, 1, 1, 'ocr')[0]
        assert row['has_text'] and row['source'] == 'native' and row['warning'] == 'OCR unavailable'
