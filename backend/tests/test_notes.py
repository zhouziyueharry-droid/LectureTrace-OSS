"""视频笔记 HTTP/并发保护测试；不调用模型。"""
import dataclasses
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import artifacts, db
from app.main import app
from app.qa import conversations
from app.qa import notes

VID = 'abc123def456'


@pytest.fixture
def client(tmp_settings):
    db.execute('INSERT INTO videos (id,title,filename,created_at,status) VALUES (?,?,?,?,?)',
               (VID, '课', 'lesson.mp4', db.now_iso(), 'done'))
    return TestClient(app)


def test_notes_survive_conversations_and_undo_checks_revision(client):
    url = f'/api/videos/{VID}/notes'
    created = client.post(url, json={'name': '公式.md'} )
    assert created.status_code == 200, created.text
    old = created.json()
    saved = client.put(url + '/公式.md', json={'text': '# 公式\n$x^2$', 'base_revision': old['revision']})
    assert saved.status_code == 200
    current = saved.json()
    assert current['can_undo']
    assert client.put(url + '/公式.md', json={'text': '覆盖', 'base_revision': old['revision']}).status_code == 409
    conversations.current(VID)
    conversations.create(VID)
    conversations.delete_current(VID)
    assert client.get(url + '/公式.md').json()['text'] == '# 公式\n$x^2$'
    assert (artifacts.video_dir(VID) / 'notes/公式.md').read_text(encoding='utf-8') == '# 公式\n$x^2$'
    assert client.post(url + '/公式.md/undo', json={'base_revision': old['revision']}).status_code == 409
    assert client.post(url + '/公式.md/undo', json={'base_revision': current['revision']}).json()['text'] == ''
    assert client.post(url, json={'name': '公式.md'}).status_code == 409
    assert [n['name'] for n in client.get(url).json()] == ['公式.md']


@pytest.mark.parametrize('name', ['../bad.md', 'a/b.md', 'a\\b.md', 'C:bad.md', 'CON.md', 'x.txt', '.hidden.md', 'x.md.'])
def test_reject_invalid_names(client, name):
    assert client.post(f'/api/videos/{VID}/notes', json={'name': name}).status_code == 400


def test_no_note_written_for_missing_video(client):
    assert client.post('/api/videos/missing/notes', json={'name': 'test.md'}).status_code == 404


def test_two_writers_only_one_can_save(client):
    original = notes.save(VID, '并发.md', 'original', None)
    def write(text):
        try:
            return notes.save(VID, '并发.md', text, original['revision'])['text']
        except notes.NoteError as error:
            return error.status_code
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(write, ['human', 'agent']))
    assert results.count(409) == 1
    assert notes.read(VID, '并发.md')['text'] in ['human', 'agent']


def test_limits_and_windows_aliases(client, tmp_settings, set_settings):
    set_settings(dataclasses.replace(tmp_settings, notes=dataclasses.replace(tmp_settings.notes, max_bytes=8)))
    with pytest.raises(notes.NoteError):
        notes.save(VID, 'big.md', '字' * 3, None)
    for name in ('con .md', 'file..md'):
        with pytest.raises(notes.NoteError):
            notes.save(VID, name, '', None)


def test_hard_link_is_not_read_or_written(client, tmp_path):
    outside = tmp_path / 'private.md'
    outside.write_text('outside', encoding='utf-8')
    dest = artifacts.video_dir(VID) / 'notes/link.md'
    dest.parent.mkdir(parents=True)
    os.link(outside, dest)
    with pytest.raises(notes.NoteError):
        notes.read(VID, 'link.md')
    assert outside.read_text() == 'outside'


def test_linked_notes_directory_cannot_escape_video(client, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    link = artifacts.video_dir(VID) / 'notes'
    link.parent.mkdir(parents=True)
    if os.name == 'nt':
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)], check=True, capture_output=True)
    else:
        link.symlink_to(outside, target_is_directory=True)
    response = client.post(f'/api/videos/{VID}/notes', json={'name': 'escape.md'})
    assert response.status_code == 400
    assert not list(outside.iterdir())


def test_legacy_copy_is_idempotent_across_root_migration(client, tmp_path):
    first, second = tmp_path / 'first', tmp_path / 'second'
    (first / 'notes/nested').mkdir(parents=True)
    (first / 'notes/nested/旧稿.md').write_text('old', encoding='utf-8')
    shutil.copytree(first, second)
    notes.import_legacy(VID, 'c1', first)
    name = notes.listing(VID)[0]['name']
    note = notes.read(VID, name)
    notes.save(VID, name, 'edited', note['revision'])
    notes.import_legacy(VID, 'c1', second)
    assert len(notes.listing(VID)) == 1
    assert notes.read(VID, name)['text'] == 'edited'
    assert (first / 'notes/nested/旧稿.md').read_text(encoding='utf-8') == 'old'


def test_manual_edit_does_not_revalidate_stale_citations(client, monkeypatch):
    monkeypatch.setattr(notes, '_evidence', lambda _: ('v1', {'T0001': {'jump': 1}}))
    original = notes.save(VID, '引用.md', '说明 [T0001]', None)
    monkeypatch.setattr(notes, '_evidence', lambda _: ('v2', {'T0001': {'jump': 99}}))
    assert notes.read(VID, '引用.md')['stale']
    updated = notes.save(VID, '引用.md', '说明 [T0001]\n我的补充', original['revision'])
    assert updated['stale'] and not updated['citations']


def test_existing_range_note_resolves_without_rewriting_and_keeps_stale_guard(client, monkeypatch):
    index = {f'T{i:04}': {'jump': i * 10} for i in range(152, 155)}
    monkeypatch.setattr(notes, '_evidence', lambda _: ('v1', index))
    original = notes.save(VID, 'range.md', 'Existing note [T0152-T0154]', None)
    path = artifacts.video_dir(VID) / 'notes/range.md'
    before = path.read_bytes()
    result = client.get(f'/api/videos/{VID}/notes/range.md').json()
    assert result['citations'] == [{'id': key, 'start': value['jump']} for key, value in index.items()]
    assert result['text'] == original['text'] and result['revision'] == original['revision']
    assert path.read_bytes() == before
    monkeypatch.setattr(notes, '_evidence', lambda _: ('v2', index))
    stale = client.get(f'/api/videos/{VID}/notes/range.md').json()
    assert stale['stale'] and stale['citations'] == []
