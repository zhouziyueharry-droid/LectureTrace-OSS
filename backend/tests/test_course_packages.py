"""Real ZIP round trips across isolated data roots. No model calls."""
import dataclasses
from contextlib import contextmanager
import hashlib
import io
import json
import os
import shutil
import stat
import zipfile
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app import artifacts, course_packages as packages, db
from app.main import app
from app.pipeline import roi, runner
from app.pipeline.base import STAGES
from app.qa import notes

VID = 'abc123def456'
CID = 'a' * 32


@pytest.fixture
def lesson(tmp_settings, monkeypatch):
    monkeypatch.setattr(runner, 'enqueue', Mock(side_effect=AssertionError('must not enqueue')))
    db.execute('INSERT INTO courses VALUES (?,?,?)', (CID, '机器学习', db.now_iso()))
    db.execute('INSERT INTO videos VALUES (?,?,?,?,?,?,?)', (VID, '第一讲', 'lecture.mp4', 2.0, db.now_iso(), 'done', CID))
    for name in STAGES:
        db.execute('INSERT INTO stages (video_id,name,status) VALUES (?,?,?)', (VID, name, 'done'))
    root = artifacts.video_dir(VID)
    root.mkdir(parents=True)
    (root / 'source.mp4').write_bytes(b'fake video for transfer only')
    (root / 'audio.wav').write_bytes(b'RIFF')
    (root / 'keyframes').mkdir()
    (root / 'keyframes/S001.jpg').write_bytes(b'\xff\xd8image')
    for name, data in {
        'audio.json': {'duration_s': 2.0},
        'transcript.json': {'language': 'en', 'segments': []},
        'cues.json': [{'id': 'C0001', 'start': 0, 'end': 2, 'text': 'Hello', 'zh': '你好'}],
        'keyframes.json': [{'id': 'S001', 'image': 'keyframes/S001.jpg', 'appearances': [[0, 2]]}],
        'segments.json': {'sections': [], 'speech': []},
        'slides.json': [],
        'evidence.json': {'pages': [], 'sections': [], 'speech': []},
        'outline.json': {'revision': 'unchanged', 'chapters': [], 'warnings': []},
    }.items():
        artifacts.write_artifact(VID, name, data, {})
    for name in ('subs.source.vtt', 'subs.bilingual.vtt'):
        (root / name).write_text('WEBVTT\n\n00:00.000 --> 00:02.000\nHello\n', encoding='utf-8')
    roi.save_confirmation(VID, [0.1, 0.1, 0.8, 0.8])
    notes.create(VID, name='笔记.md', text='# Note')
    notes.create(VID, name='回收.md', text='trash')
    trash = notes.delete(VID, '回收.md')
    db.set_setting('openai_api_key', 'do-not-export')
    (root / '.env').write_text('do-not-export')
    return tmp_settings, root, trash


def exported(client):
    response = client.post(f'/api/videos/{VID}/package')
    assert response.status_code == 200, response.text
    result = response.json()
    response = client.get(result['download_url'])
    assert response.status_code == 200
    assert client.get(result['download_url'], headers={'Range': 'bytes=0-9'}).content == response.content[:10]
    assert client.delete(f"/api/packages/{result['token']}").status_code == 200
    return response.content


@pytest.fixture
def whole_course(lesson):
    second = 'def456abc123'
    db.execute('INSERT INTO videos VALUES (?,?,?,?,?,?,?)',
               (second, '第二讲', 'second.mp4', 2.0, db.now_iso(), 'done', CID))
    for name in STAGES:
        db.execute('INSERT INTO stages (video_id,name,status) VALUES (?,?,?)', (second, name, 'done'))
    shutil.copytree(lesson[1], artifacts.video_dir(second))
    return lesson[0], [VID, second]


def exported_course(client):
    response = client.post(f'/api/courses/{CID}/package')
    assert response.status_code == 200, response.text
    result = response.json()
    raw = client.get(result['download_url']).content
    client.delete(f"/api/packages/{result['token']}")
    return raw


@pytest.mark.parametrize('scope', ['videos', 'courses'])
def test_export_stream_reports_real_bytes(whole_course, scope):
    client = TestClient(app)
    identity = VID if scope == 'videos' else CID
    response = client.post(f'/api/{scope}/{identity}/package', headers={'Accept': 'text/event-stream'})
    assert response.headers['content-type'].startswith('text/event-stream')
    events = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith('data:')]
    assert events[0]['type'] == 'progress'
    assert events[-1]['type'] == 'done'
    progress = [e for e in events if e['type'] == 'progress']
    assert {'waiting', 'scanning', 'writing', 'verifying'} <= {p['phase'] for p in progress}
    assert [p['written_bytes'] for p in progress] == sorted(p['written_bytes'] for p in progress)
    result = events[-1]['result']
    with zipfile.ZipFile(packages.stage_path(result['token']) / 'export.ltcourse') as archive:
        total = sum(i.file_size for i in archive.infolist() if i.filename != 'manifest.json')
    assert progress[-1]['written_bytes'] == progress[-1]['total_bytes'] == total
    assert progress[-1]['lecture_count'] == (1 if scope == 'videos' else 2)
    assert client.get(result['download_url']).status_code == 200


def test_export_stream_error_has_no_download(whole_course):
    db.execute("UPDATE videos SET status='failed' WHERE id=?", (VID,))
    response = TestClient(app).post(f'/api/courses/{CID}/package', headers={'Accept': 'text/event-stream'})
    events = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith('data:')]
    assert events[-1]['type'] == 'error' and events[-1]['status'] == 409
    assert '第一讲' in events[-1]['detail']
    assert not any(e['type'] == 'done' for e in events)
    assert not list(packages.staging_root().iterdir())


def test_whole_course_roundtrip(whole_course, tmp_path, set_settings):
    original, ids = whole_course
    client = TestClient(app)
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    pdf = io.BytesIO()
    writer.write(pdf)
    response = client.post(f'/api/videos/{VID}/pdfs', params={'name': '讲义.pdf'}, content=pdf.getvalue())
    assert response.status_code == 200, response.text
    raw = exported_course(client)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        expected = {n: z.read(n) for n in z.namelist() if n != 'manifest.json'}
        assert json.loads(z.read('manifest.json'))['version'] == 3
        assert not any(n.endswith('.env') for n in expected)
    set_settings(dataclasses.replace(original, data_dir=tmp_path / 'destination'))
    db.init_db()
    response = client.post('/api/packages/preview', content=raw)
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview['course_id'] == CID and preview['title'] == '机器学习'
    assert [v['video_id'] for v in preview['lectures']] == ids
    assert preview['note_count'] == 2 and not preview['duplicate']
    assert 'PDF 资料' in preview['contents']
    # A duplicate appearing after preview must block the entire import.
    db.execute('INSERT INTO videos VALUES (?,?,?,?,?,?,?)', (ids[1], '已有笔记的讲次', 'local.mp4', 1, db.now_iso(), 'done', None))
    assert client.post(f"/api/packages/{preview['token']}/import").status_code == 409
    assert not artifacts.video_dir(ids[0]).exists()
    assert db.fetch_one('SELECT title FROM videos WHERE id=?', (ids[1],))['title'] == '已有笔记的讲次'
    db.execute('DELETE FROM videos WHERE id=?', (ids[1],))
    # One name conflict must create exactly one new course for every lecture.
    db.execute('INSERT INTO courses VALUES (?,?,?)', (CID, '本机另一门课', db.now_iso()))
    result = client.post(f"/api/packages/{preview['token']}/import")
    assert result.status_code == 200, result.text
    restored_course = result.json()['course_id']
    assert restored_course != CID
    assert result.json() == {'course_id': restored_course, 'video_ids': ids}
    assert db.fetch_one('SELECT COUNT(*) AS n FROM courses')['n'] == 2
    for video_id in ids:
        target = artifacts.video_dir(video_id)
        actual = {f'videos/{video_id}/{p.relative_to(target).as_posix()}': p.read_bytes()
                  for p in target.rglob('*') if p.is_file()}
        assert actual == {n: value for n, value in expected.items() if n.startswith(f'videos/{video_id}/')}
        assert db.fetch_one('SELECT status,course_id FROM videos WHERE id=?', (video_id,)) == {'status': 'done', 'course_id': restored_course}
    runner.enqueue.assert_not_called()
    duplicate = client.post('/api/packages/preview', content=raw).json()
    assert duplicate['duplicate']
    assert client.post(f"/api/packages/{duplicate['token']}/import").status_code == 409


def test_whole_course_incomplete_and_empty_rejected(whole_course):
    _, ids = whole_course
    client = TestClient(app)
    db.execute("UPDATE videos SET status='failed' WHERE id=?", (ids[1],))
    response = client.post(f'/api/courses/{CID}/package')
    assert response.status_code == 409 and '第二讲' in response.text
    db.execute('UPDATE videos SET course_id=NULL')
    assert client.post(f'/api/courses/{CID}/package').status_code == 409
    assert client.post('/api/courses/not-a-course/package').status_code == 404
    assert not list(packages.staging_root().iterdir())


def test_whole_course_aggregate_limits(whole_course, set_settings):
    original, _ = whole_course
    client = TestClient(app)
    raw = exported_course(client)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        manifest = json.loads(archive.read('manifest.json'))
    for limit, value in [('max_files', sum(len(v['files']) for v in manifest['lectures']) - 1),
                         ('max_expanded_bytes', sum(e['size'] for v in manifest['lectures'] for e in v['files'].values()) - 1)]:
        set_settings(dataclasses.replace(original, packages=dataclasses.replace(original.packages, **{limit: value})))
        assert client.post('/api/packages/preview', content=raw).status_code == 400
        assert client.post(f'/api/courses/{CID}/package').status_code in (400, 413)
        assert not list(packages.staging_root().iterdir())


def test_whole_course_changes_during_export(whole_course, monkeypatch):
    write = zipfile.ZipFile.writestr
    def change_course(self, *args, **kwargs):
        write(self, *args, **kwargs)
        db.execute('UPDATE videos SET course_id=NULL WHERE id=?', (VID,))
    monkeypatch.setattr(zipfile.ZipFile, 'writestr', change_course)
    assert TestClient(app).post(f'/api/courses/{CID}/package').status_code == 409
    assert not list(packages.staging_root().iterdir())


def test_whole_course_rolls_back_all_lectures(whole_course, tmp_path, set_settings):
    original, ids = whole_course
    client = TestClient(app)
    raw = exported_course(client)
    set_settings(dataclasses.replace(original, data_dir=tmp_path / 'destination'))
    db.init_db()
    response = client.post('/api/packages/preview', content=raw)
    assert response.status_code == 200, response.text
    token = response.json()['token']
    db.execute(f"CREATE TRIGGER fail_second BEFORE INSERT ON videos WHEN NEW.id='{ids[1]}' BEGIN SELECT RAISE(ABORT, 'test'); END")
    assert client.post(f'/api/packages/{token}/import').status_code == 500
    assert db.fetch_all('SELECT * FROM videos') == []
    assert db.fetch_all('SELECT * FROM courses') == []
    assert all(not artifacts.video_dir(v).exists() for v in ids)
    assert all((packages.stage_path(token) / 'payload' / 'videos' / v).is_dir() for v in ids)
    db.execute('DROP TRIGGER fail_second')
    assert client.post(f'/api/packages/{token}/import').status_code == 200


@pytest.mark.parametrize('kind', ['duplicate_id', 'course_mismatch', 'path', 'missing', 'empty'])
def test_whole_course_rejects_invalid_manifest(whole_course, kind):
    client = TestClient(app)
    raw = exported_course(client)
    def modify(data):
        m = json.loads(data['manifest.json'])
        if kind == 'duplicate_id':
            m['lectures'][1] = m['lectures'][0]
        elif kind == 'course_mismatch':
            m['lectures'][1]['course']['id'] = 'b' * 32
        elif kind == 'path':
            m['lectures'][0]['files']['../outside'] = m['lectures'][0]['files']['source.mp4']
        elif kind == 'empty':
            m['lectures'] = []
        else:
            del data[f'videos/{VID}/source.mp4']
        data['manifest.json'] = json.dumps(m).encode()
    assert client.post('/api/packages/preview', content=rewrite(raw, modify)).status_code == 400
    assert not list(packages.staging_root().iterdir())


def rewrite(raw, modify):
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        data = {n: z.read(n) for n in z.namelist()}
    modify(data)
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as z:
        for n, value in data.items():
            z.writestr(n, value)
    return output.getvalue()


def test_roundtrip_two_data_roots(lesson, tmp_path, set_settings):
    original, root, trash = lesson
    client = TestClient(app)
    raw = exported(client)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        expected = {n: z.read(n) for n in z.namelist() if n != 'manifest.json'}
        assert '.env' not in z.namelist()
        assert b'do-not-export' not in z.read('manifest.json')
    assert not list(packages.staging_root().iterdir())
    set_settings(dataclasses.replace(original, data_dir=tmp_path / 'mac'))
    db.init_db()
    response = client.post('/api/packages/preview', content=raw)
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview['title'] == '第一讲' and preview['note_count'] == 1 and not preview['duplicate']
    assert db.fetch_all('SELECT * FROM videos') == []
    assert client.post(f"/api/packages/{preview['token']}/import").json() == {'video_id': VID}
    target = artifacts.video_dir(VID)
    assert {p.relative_to(target).as_posix(): p.read_bytes() for p in target.rglob('*') if p.is_file()} == expected
    assert db.fetch_one('SELECT status,course_id FROM videos WHERE id=?', (VID,)) == {'status': 'done', 'course_id': CID}
    assert roi.confirmed_roi(VID) == [0.1, 0.1, 0.8, 0.8]
    assert db.get_setting('openai_api_key') is None
    assert notes.read(VID, '笔记.md')['text'] == '# Note'
    assert notes.versions(VID, '笔记.md')
    assert notes.restore_trash(VID, trash['trash_id'])['text'] == 'trash'
    runner.recover()  # A restart also must not enqueue imported completed videos.
    runner.enqueue.assert_not_called()
    assert client.get(f'/api/videos/{VID}/outline').status_code == 200
    assert client.get(f'/api/videos/{VID}/subtitles/source.vtt').status_code == 200
    assert client.get(f'/api/videos/{VID}/file').content == expected['source.mp4']


def test_duplicate_never_overwrites(lesson):
    client = TestClient(app)
    raw = exported(client)
    response = client.post('/api/packages/preview', content=raw)
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview['duplicate']
    assert client.post(f"/api/packages/{preview['token']}/import").status_code == 409
    assert client.delete(f"/api/packages/{preview['token']}").status_code == 200
    assert db.fetch_one('SELECT COUNT(*) AS n FROM videos')['n'] == 1


@pytest.mark.parametrize('bad_path', ['../outside.txt', '/outside.txt', 'C:/outside.txt', 'notes/a\\b.md', 'notes/CON.md', 'notes/x.md.', 'keyframes/../../escape'])
def test_reject_paths(lesson, bad_path):
    client = TestClient(app)
    raw = exported(client)
    def modify(data):
        m = json.loads(data['manifest.json'])
        m['files'][bad_path] = {'size': 1, 'sha256': hashlib.sha256(b'x').hexdigest()}
        data[bad_path] = b'x'
        data['manifest.json'] = json.dumps(m).encode()
    response = client.post('/api/packages/preview', content=rewrite(raw, modify))
    assert response.status_code == 400, response.text
    assert not list(packages.staging_root().iterdir())


@pytest.mark.parametrize('kind', ['version', 'corrupt', 'missing', 'invalid_json', 'case_collision', 'stages', 'image_path'])
def test_invalid_packages(lesson, kind):
    client = TestClient(app)
    raw = exported(client)
    def modify(data):
        m = json.loads(data['manifest.json'])
        if kind == 'version':
            m['version'] = 999
        elif kind == 'corrupt':
            data['source.mp4'] += b'x'
        elif kind == 'missing':
            del data['source.mp4']
            del m['files']['source.mp4']
        elif kind == 'stages':
            m['stages'][0]['status'] = 'running'
        elif kind == 'case_collision':
            for n in ('notes/a.md', 'notes/A.md'):
                data[n] = b'x'
                m['files'][n] = {'size': 1, 'sha256': hashlib.sha256(b'x').hexdigest()}
        else:
            name = 'keyframes.json'
            value = b'bad-json' if kind == 'invalid_json' else json.dumps({'schema_version': 1, 'data': [{'image': '../../secret'}]}).encode()
            data[name] = value
            m['files'][name] = {'size': len(value), 'sha256': hashlib.sha256(value).hexdigest()}
        data['manifest.json'] = json.dumps(m).encode()
    response = client.post('/api/packages/preview', content=rewrite(raw, modify))
    assert response.status_code == 400, response.text
    assert not list(packages.staging_root().iterdir())


def test_reject_symlink_and_duplicate_zip_entries(lesson):
    client = TestClient(app)
    raw = exported(client)
    for symlink in (True, False):
        stream = io.BytesIO(raw)
        with zipfile.ZipFile(stream, 'a') as z:
            if symlink:
                info = zipfile.ZipInfo('source.mp4')
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                z.writestr(info, '/private')
            else:
                z.writestr('source.mp4', 'duplicate')
        assert client.post('/api/packages/preview', content=stream.getvalue()).status_code == 400


def test_limits_and_active_export(lesson, set_settings):
    original, _, _ = lesson
    client = TestClient(app)
    raw = exported(client)
    set_settings(dataclasses.replace(original, packages=dataclasses.replace(original.packages, max_archive_bytes=10)))
    assert client.post('/api/packages/preview', content=raw).status_code == 413
    set_settings(original)
    db.execute("UPDATE videos SET status='processing' WHERE id=?", (VID,))
    assert client.post(f'/api/videos/{VID}/package').status_code == 409


def test_cancel_and_expiry(lesson):
    client = TestClient(app)
    raw = exported(client)
    token = client.post('/api/packages/preview', content=raw).json()['token']
    assert client.delete(f'/api/packages/{token}').status_code == 200
    assert client.post(f'/api/packages/{token}/import').status_code == 404
    token, path = packages.new_stage()
    os.utime(path, (1, 1))
    next_token, _ = packages.new_stage()
    assert not path.exists()
    packages.discard(next_token)


def test_old_fullframe_and_skipped_reason_survive_target_defaults(lesson, tmp_path, set_settings):
    original, _, _ = lesson
    roi.delete(VID)
    db.execute("UPDATE stages SET status='skipped',error='未开启大纲生成' WHERE video_id=? AND name='outline'", (VID,))
    client = TestClient(app)
    raw = exported(client)
    set_settings(dataclasses.replace(original, data_dir=tmp_path / 'target'))
    db.init_db()
    db.set_setting('keyframe_roi_default', [0.1, 0.2, 0.5, 0.5])
    token = client.post('/api/packages/preview', content=raw).json()['token']
    assert client.post(f'/api/packages/{token}/import').status_code == 200
    assert roi.confirmed_roi(VID) == roi.FULL_FRAME
    assert roi.effective(VID) is None
    assert db.fetch_one("SELECT error FROM stages WHERE video_id=? AND name='outline'", (VID,))['error'] == '未开启大纲生成'


def test_reject_excessive_expansion(lesson, set_settings):
    original, _, _ = lesson
    client = TestClient(app)
    raw = exported(client)
    set_settings(dataclasses.replace(original, packages=dataclasses.replace(original.packages, max_expanded_bytes=2)))
    assert client.post('/api/packages/preview', content=raw).status_code == 400
    assert not list(packages.staging_root().iterdir())


def test_database_failure_keeps_library_unchanged(lesson, tmp_path, set_settings, monkeypatch):
    original, _, _ = lesson
    client = TestClient(app)
    raw = exported(client)
    set_settings(dataclasses.replace(original, data_dir=tmp_path / 'destination'))
    db.init_db()
    token = client.post('/api/packages/preview', content=raw).json()['token']
    # A constraint failure after inserting the course must roll back both rows.
    db.execute("CREATE TRIGGER fail_import BEFORE INSERT ON stages BEGIN SELECT RAISE(ABORT, 'test'); END")
    with pytest.raises(Exception, match='test'):
        packages.commit(token)
    assert db.fetch_all('SELECT * FROM videos') == []
    assert db.fetch_all('SELECT * FROM courses') == []
    assert not artifacts.video_dir(VID).exists()
    assert (packages.stage_path(token) / 'payload').is_dir()


def test_commit_failure_after_directory_move_rolls_back(lesson, tmp_path, set_settings, monkeypatch):
    original, _, _ = lesson
    client = TestClient(app)
    raw = exported(client)
    set_settings(dataclasses.replace(original, data_dir=tmp_path / 'destination'))
    db.init_db()
    token = client.post('/api/packages/preview', content=raw).json()['token']
    real_connect = db.connect
    @contextmanager
    def fail_commit():
        with real_connect() as conn:
            yield conn
            if artifacts.video_dir(VID).exists():
                raise OSError('simulated commit failure')
    monkeypatch.setattr(db, 'connect', fail_commit)
    assert client.post(f'/api/packages/{token}/import').status_code == 400
    assert not artifacts.video_dir(VID).exists()
    assert db.fetch_all('SELECT * FROM videos') == []
    assert db.fetch_all('SELECT * FROM courses') == []
    assert (packages.stage_path(token) / 'payload').is_dir()
