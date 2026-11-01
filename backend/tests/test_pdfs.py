import io
import dataclasses
import json
import subprocess
from unittest.mock import patch

import pytest

from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app import artifacts, db
from app.main import app
from test_codex_workspace import course, build
from test_course_packages import lesson, exported

VID = 'abc123def456'


def sample_pdf(encrypted=False):
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'),
                             NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
    stream = DecodedStreamObject()
    stream.set_data(b'BT /F1 18 Tf 50 700 Td (Gradient descent learns parameters.) Tj ET')
    page[NameObject('/Contents')] = writer._add_object(stream)
    writer.add_blank_page(width=600, height=800)
    if encrypted:
        writer.encrypt('secret')
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_extract_pdf_from_deep_package_directory(tmp_path, tmp_settings):
    from app import pdfs

    directory = tmp_path
    while len(str(directory / 'source.pdf')) < 235:
        directory /= 'package-data'
    directory.mkdir(parents=True)
    source = directory / 'source.pdf'
    raw = sample_pdf()
    source.write_bytes(raw)

    assert pdfs.extract(source) == ['Gradient descent learns parameters.', '']
    assert source.read_bytes() == raw
    assert list(directory.iterdir()) == [source]


def test_pdf_lifecycle_and_validation(tmp_settings, set_settings):
    db.execute('INSERT INTO videos VALUES (?,?,?,?,?,?,?)', (VID, 'PDF lesson', 'lesson.mp4', 1, db.now_iso(), 'done', None))
    artifacts.video_dir(VID).mkdir(parents=True)
    client = TestClient(app)
    url = f'/api/videos/{VID}/pdfs'
    assert client.get(f'{url}/trash').json() == []
    raw = sample_pdf()
    response = client.post(url, params={'name': '课程.pdf'}, content=raw)
    assert response.status_code == 200, response.text
    item = response.json()
    assert item['page_count'] == 2 and item['text_pages'] == 1
    file_url = f"{url}/{item['id']}/file"
    assert client.get(file_url).content == raw
    assert client.get(file_url, headers={'Range': 'bytes=0-9'}).content == raw[:10]
    assert client.get(file_url, params={'revision': 'wrong'}).status_code == 409
    assert client.post(url, params={'name': '课程.pdf'}, content=raw).json()['id'] != item['id']
    assert client.post(url, params={'name': 'bad.pdf'}, content=b'not PDF').status_code == 400
    assert client.post(url, params={'name': 'lock.pdf'}, content=sample_pdf(True)).status_code == 400
    assert client.post(url, params={'name': '../bad.pdf'}, content=raw).status_code == 400
    assert client.get(f'/api/videos/absent/pdfs/{item["id"]}/file').status_code == 404
    assert client.delete(f"{url}/{item['id']}").status_code == 200
    assert client.get(f'{url}/trash').json() == [{'trash_id': item['id'], 'name': item['name']}]
    assert client.get(file_url).status_code == 404
    assert client.post(f"{url}/trash/{item['id']}/restore").status_code == 200
    assert client.get(f'{url}/trash').json() == []
    assert client.get(file_url).content == raw
    set_settings(dataclasses.replace(tmp_settings, pdfs=dataclasses.replace(tmp_settings.pdfs, max_bytes=20)))
    assert client.post(url, params={'name': 'big.pdf'}, content=raw).status_code == 413
    assert not list((artifacts.video_dir(VID) / 'pdfs').glob('.upload-*'))


def test_agent_pdf_ignores_retired_budget_and_validates_citations(course, monkeypatch):
    from app import pdfs
    from app.qa import codex_chat, notes
    db.execute('INSERT INTO videos VALUES (?,?,?,?,?,?,?)', (VID, 'PDF lesson', 'lesson.mp4', 1, db.now_iso(), 'done', None))
    client = TestClient(app)
    url = f'/api/videos/{VID}/pdfs'
    item = client.post(url, params={'name': 'lecture.pdf'}, content=sample_pdf()).json()
    ws = build(course)
    args = {'id': item['id'], 'revision': item['revision'], 'start_page': 1, 'end_page': 2}
    cite = f"PDF:{item['id']}:{item['revision']}:1"
    assert codex_chat.resolve(f'[{cite}]', ws)[2] == [cite]
    page_map = ws.call('pdf_page_map', {'id': item['id'], 'revision': item['revision']})
    pages = json.loads(page_map['contentItems'][0]['text'])['pages']
    assert page_map['success'] and pages[0]['text_chars'] > 0 and pages[1]['text_chars'] == 0
    assert not any(page['text_read'] or page['image_viewed'] for page in pages)
    assert not ws.pdf_read_ids  # navigation is never citation proof
    ws.options['max_read_tokens'] = 1
    result = ws.call('read_pdf', args)
    assert result['success'] and 'Gradient descent' in result['contentItems'][0]['text']
    assert ws.pdf_read_ids == {cite}
    pages = json.loads(ws.call('pdf_page_map', {'id': item['id'], 'revision': item['revision']})['contentItems'][0]['text'])['pages']
    assert pages[0]['text_read'] and not pages[0]['image_viewed'] and not pages[1]['text_read']
    monkeypatch.setattr(pdfs, 'view_page', lambda *args: {'image': 'eA=='})
    ws.call('view_pdf_page', {'id': item['id'], 'revision': item['revision'], 'page': 1,
                              'crop': [0, 0, 0.5, 0.5]})
    pages = json.loads(ws.call('pdf_page_map', {'id': item['id'], 'revision': item['revision']})['contentItems'][0]['text'])['pages']
    assert not pages[0]['image_viewed']
    ws.call('view_pdf_page', {'id': item['id'], 'revision': item['revision'], 'page': 1})
    pages = json.loads(ws.call('pdf_page_map', {'id': item['id'], 'revision': item['revision']})['contentItems'][0]['text'])['pages']
    assert pages[0]['image_viewed']
    assert codex_chat.resolve(f'[{cite}]', ws)[1][0]['page'] == 1
    assert codex_chat.resolve(f"[PDF:{item['id']}:{item['revision']}:2]", ws)[2]
    assert build(course).pdf_read_ids == {cite}
    assert not build(course, 'other').pdf_read_ids
    assert not ws.call('read_pdf', {**args, 'start_page': 0})['success']
    assert not ws.call('read_pdf', {**args, 'revision': 'wrong'})['success']
    assert not ws.call('pdf_page_map', {'id': item['id'], 'revision': 'wrong'})['success']
    result = ws.call('write_note', {'path': 'notes/pdf.md', 'text': f'Learned [{cite}]', 'base_revision': None})
    assert result['success'], result
    assert notes.read(VID, 'pdf.md')['citations'][0]['pdf_id'] == item['id']
    ws.options['max_tool_calls'] = ws.calls
    assert ws.call('read_pdf', args)['success']
    pdfs.delete(VID, item['id'])
    assert codex_chat.resolve(f'[{cite}]', ws)[2]
    assert not notes.read(VID, 'pdf.md')['citations']


def test_pdf_package_roundtrip(lesson, tmp_path, set_settings):
    import json
    import zipfile
    from app import pdfs
    original, _, _ = lesson
    client = TestClient(app)
    url = f'/api/videos/{VID}/pdfs'
    raw_pdf = sample_pdf()
    item = client.post(url, params={'name': '讲义.pdf'}, content=raw_pdf).json()
    archived = client.post(url, params={'name': '回收.pdf'}, content=raw_pdf).json()
    pdfs.delete(VID, archived['id'])
    raw = exported(client)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert json.loads(archive.read('manifest.json'))['version'] == 2
        assert not any(n.endswith('pages.json') for n in archive.namelist())
    set_settings(dataclasses.replace(original, data_dir=tmp_path / 'destination'))
    db.init_db()
    response = client.post('/api/packages/preview', content=raw)
    assert response.status_code == 200, response.text
    token = response.json()['token']
    assert client.post(f'/api/packages/{token}/import').status_code == 200
    assert client.get(f"{url}/{item['id']}/file").content == raw_pdf
    assert pdfs.read_pages(VID, item['id'], item['revision'], 1, 1)[0]['text'].startswith('Gradient')
    assert pdfs.restore(VID, archived['id'])['revision'] == archived['revision']


def test_pdf_resource_limits_and_links(tmp_settings, tmp_path, set_settings):
    from app import pdfs
    from app.qa import notes
    source = tmp_path / 'sample.pdf'
    source.write_bytes(sample_pdf())
    set_settings(dataclasses.replace(tmp_settings, pdfs=dataclasses.replace(tmp_settings.pdfs, max_pages=1)))
    with pytest.raises(notes.NoteError, match='页数'):
        pdfs.extract(source)
    set_settings(dataclasses.replace(tmp_settings, pdfs=dataclasses.replace(tmp_settings.pdfs, max_text_bytes=2)))
    with pytest.raises(notes.NoteError, match='文字'):
        pdfs.extract(source)
    with patch('app.pdfs.subprocess.run', side_effect=subprocess.TimeoutExpired('worker', 1)):
        with pytest.raises(notes.NoteError, match='超时'):
            pdfs.extract(source)
    assert not list(tmp_path.glob('.extract-*'))
    set_settings(tmp_settings)
    db.execute('INSERT INTO videos VALUES (?,?,?,?,?,?,?)', (VID, 'PDF lesson', 'lesson.mp4', 1, db.now_iso(), 'done', None))
    artifacts.video_dir(VID).mkdir(parents=True)
    client = TestClient(app)
    item = client.post(f'/api/videos/{VID}/pdfs', params={'name':'test.pdf'}, content=source.read_bytes()).json()
    with pytest.raises(notes.NoteError, match='revision'):
        pdfs.read_pages(VID, item['id'], None, 1, 1)
    stored = pdfs.folder(VID, item['id']) / 'source.pdf'
    import os
    os.link(stored, tmp_path / 'linked.pdf')
    with pytest.raises(notes.NoteError):
        pdfs.metadata(VID, item['id'])
    (tmp_path / 'linked.pdf').unlink()
    stored.write_bytes(b'changed')
    with pytest.raises(notes.NoteError, match='原件已变化'):
        pdfs.metadata(VID, item['id'])
