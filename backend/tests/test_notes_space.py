"""笔记空间（0011）：标题转文件名、版本历史、重命名、回收站、Agent 建议；不调用模型。"""
import dataclasses
import time

import pytest
from fastapi.testclient import TestClient

from app import artifacts, db
from app.main import app
from app.qa import notes

VID = 'abc123def456'
EVIDENCE = ('sha256:ev1', {'T0001': {'jump': 3.0, 'kind': 'speech'}})


@pytest.fixture
def client(tmp_settings, monkeypatch):
    db.execute('INSERT INTO videos (id,title,filename,created_at,status) VALUES (?,?,?,?,?)',
               (VID, '课', 'lesson.mp4', db.now_iso(), 'done'))
    monkeypatch.setattr(notes, '_evidence', lambda video_id: EVIDENCE)
    return TestClient(app)


def url(suffix=''):
    return f'/api/videos/{VID}/notes{suffix}'


# ---------- 标题即文件名 ----------

def test_title_becomes_unique_valid_filename(client):
    first = client.post(url(), json={'title': '  公式: 推导/笔记?  '}).json()
    assert first['name'] == '公式 推导 笔记.md'
    assert client.post(url(), json={}).json()['name'] == '无标题.md'
    assert client.post(url(), json={'title': ''}).json()['name'] == '无标题 2.md'
    assert client.post(url(), json={'title': 'CON'}).json()['name'] == 'CON 页.md'
    long = client.post(url(), json={'title': 'a' * 400})
    assert long.status_code == 200 and len(long.json()['name']) == 63


def test_create_with_text_and_explicit_name_conflict(client):
    note = client.post(url(), json={'title': '副本', 'text': '# 大纲\n内容'}).json()
    assert note['text'] == '# 大纲\n内容'
    assert client.post(url(), json={'name': '副本.md'}).status_code == 409


# ---------- 版本历史 ----------

def test_user_saves_merge_inside_window_but_agent_versions_do_not(client, tmp_settings):
    note = notes.create(VID, title='史')
    for text in ('a', 'ab', 'abc'):
        note = notes.save(VID, '史.md', text, note['revision'])
    ids = [v['label'] for v in notes.versions(VID, '史.md')]
    assert ids == [None, '创建页面']  # 三次连续保存合并为一个版本
    note = notes.save(VID, '史.md', 'abcd', note['revision'], author='agent', label='Agent 修改', sealed=True)
    note = notes.save(VID, '史.md', 'abcde', note['revision'])
    versions = notes.versions(VID, '史.md')
    assert [v['author'] for v in versions] == ['user', 'agent', 'user', 'user']
    assert versions[0]['current'] and not any(v['current'] for v in versions[1:])


def test_user_versions_split_after_window_and_are_capped(client, tmp_settings, set_settings):
    set_settings(dataclasses.replace(tmp_settings, notes=dataclasses.replace(
        tmp_settings.notes, version_window_s=0, max_versions=3)))
    note = notes.create(VID, title='限')
    for i in range(6):
        note = notes.save(VID, '限.md', f'v{i}', note['revision'])
    versions = notes.versions(VID, '限.md')
    assert len(versions) == 3
    assert notes.read_version(VID, '限.md', versions[0]['id'])['text'] == 'v5'


def test_restore_creates_new_version_and_keeps_history(client):
    note = notes.create(VID, title='复')
    note = notes.save(VID, '复.md', '旧', note['revision'], author='agent', label='x', sealed=True)
    old = notes.versions(VID, '复.md')[0]
    note = notes.save(VID, '复.md', '新', note['revision'], label='y', sealed=True)
    response = client.post(url('/复.md/restore'), json={'version_id': old['id'], 'base_revision': note['revision']})
    assert response.status_code == 200 and response.json()['text'] == '旧'
    assert client.post(url('/复.md/restore'), json={'version_id': old['id'], 'base_revision': 'stale'}).status_code == 409
    assert client.get(url('/复.md/versions/../x')).status_code in (404, 405)
    assert client.get(url('/复.md/versions/not-valid')).status_code == 400
    assert len(client.get(url('/复.md/versions')).json()) == 4


def test_put_cannot_claim_agent_authorship(client):
    note = client.post(url(), json={'title': '作者'}).json()
    saved = client.put(url('/作者.md'), json={'text': 'x', 'base_revision': note['revision'], 'author': 'agent'})
    assert saved.status_code == 200
    assert client.get(url('/作者.md/versions')).json()[0]['author'] == 'user'


# ---------- 重命名与回收站 ----------

def test_rename_moves_note_history_and_proposal(client):
    note = notes.create(VID, title='旧名', text='正文')
    notes.propose(VID, '旧名.md', '正文改', note['revision'], 'sha256:ev1')
    assert client.post(url('/旧名.md/rename'), json={'new_name': '新名.md', 'base_revision': 'stale'}).status_code == 409
    renamed = client.post(url('/旧名.md/rename'), json={'new_name': '新名.md', 'base_revision': note['revision']})
    assert renamed.status_code == 200 and renamed.json()['name'] == '新名.md'
    assert client.get(url('/旧名.md')).status_code == 404
    assert client.get(url('/新名.md/proposal')).json()['name'] == '新名.md'
    assert client.get(url('/新名.md/versions')).json()
    other = notes.create(VID, title='占用')
    assert client.post(url('/新名.md/rename'), json={'new_name': '占用.md', 'base_revision': renamed.json()['revision']}).status_code == 409
    assert client.post(url('/新名.md/rename'), json={'new_name': '../x.md', 'base_revision': renamed.json()['revision']}).status_code == 400
    assert other['name'] == '占用.md'


def test_rename_allows_case_only_change(client):
    note = notes.create(VID, title='abc')
    assert notes.rename(VID, 'abc.md', 'ABC.md', note['revision'])['name'] == 'ABC.md'
    assert [n['name'] for n in notes.listing(VID)] == ['ABC.md']


def test_delete_to_trash_and_restore(client):
    assert client.get(url('/trash')).json() == []
    note = notes.create(VID, title='删', text='内容')
    notes.save(VID, '删.md', '内容二', note['revision'])
    deleted = client.delete(url('/删.md'))
    assert deleted.status_code == 200
    listing = client.get(url('/trash'))
    assert listing.status_code == 200
    assert listing.json()[0]['trash_id'] == deleted.json()['trash_id']
    assert listing.json()[0]['name'] == '删.md'
    assert listing.json()[0]['deleted_at'] > 0
    assert client.get(url()).json() == []
    assert client.get(url('/删.md')).status_code == 404
    again = notes.create(VID, name='删.md')
    assert client.post(url(f"/trash/{deleted.json()['trash_id']}/restore")).status_code == 409
    notes.delete(VID, '删.md')
    restored = client.post(url(f"/trash/{deleted.json()['trash_id']}/restore"))
    assert restored.status_code == 200 and restored.json()['text'] == '内容二'
    assert deleted.json()['trash_id'] not in [item['trash_id'] for item in client.get(url('/trash')).json()]
    assert client.get(url('/删.md/versions')).json()[0]['author'] == 'user'
    assert again['name'] == '删.md'


def test_trash_ids_are_validated_and_old_entries_purged(client, tmp_settings, set_settings):
    assert client.post(url('/trash/abc/restore')).status_code == 400
    assert client.post(url('/trash/1-deadbeef/restore')).status_code == 404
    set_settings(dataclasses.replace(tmp_settings, notes=dataclasses.replace(tmp_settings.notes, trash_days=0)))
    notes.create(VID, title='一')
    old = notes.delete(VID, '一.md')
    time.sleep(0.01)
    notes.create(VID, title='二')
    notes.delete(VID, '二.md')
    assert not (artifacts.video_dir(VID) / 'notes/.trash' / old['trash_id']).exists()


def test_agent_new_flag_until_seen(client):
    notes.save(VID, 'a.md', 'x', None, author='agent', label='Agent 新建页面', sealed=True, created_by='agent')
    assert notes.listing(VID)[0]['agent_new'] is True
    note = notes.read(VID, 'a.md')
    notes.save(VID, 'a.md', 'xy', note['revision'])  # 用户编辑不清除标记，只有打开才算已读
    assert notes.listing(VID)[0]['agent_new'] is True
    assert client.post(url('/a.md/seen')).status_code == 200
    assert notes.listing(VID)[0]['agent_new'] is False


# ---------- Agent 建议 ----------

def test_proposal_never_changes_text_until_resolved(client):
    note = notes.create(VID, title='提', text='甲\n\n乙')
    proposal = notes.propose(VID, '提.md', '甲\n\n乙改\n\n丙', note['revision'], 'sha256:ev1')
    assert notes.read(VID, '提.md')['text'] == '甲\n\n乙'
    assert [n['has_proposal'] for n in notes.listing(VID)] == [True]
    assert client.get(url('/提.md/proposal')).json()['id'] == proposal['id']
    assert notes.propose(VID, '提.md', '甲\n\n乙', note['revision'], 'sha256:ev1') is None
    with pytest.raises(notes.NoteError) as error:
        notes.propose(VID, '提.md', 'x', 'stale', 'sha256:ev1')
    assert error.value.status_code == 409


def test_resolve_applies_composed_text_with_agent_author(client):
    note = notes.create(VID, title='提', text='甲\n\n乙')
    proposal = notes.propose(VID, '提.md', '甲\n\n乙改\n\n丙 [T0001]', note['revision'], 'sha256:ev1')
    body = {'proposal_id': proposal['id'], 'text': '甲\n\n乙\n\n丙 [T0001]', 'base_revision': note['revision'], 'accepted': 1, 'total': 2}
    done = client.post(url('/提.md/proposal/resolve'), json=body)
    assert done.status_code == 200 and done.json()['text'] == '甲\n\n乙\n\n丙 [T0001]'
    assert client.get(url('/提.md/proposal')).json() is None
    latest = client.get(url('/提.md/versions')).json()[0]
    assert latest['author'] == 'agent' and '1/2' in latest['label']
    assert client.post(url('/提.md/proposal/resolve'), json=body).status_code == 409


def test_resolve_rejecting_all_discards_and_stale_evidence_fails(client, monkeypatch):
    note = notes.create(VID, title='提', text='甲')
    proposal = notes.propose(VID, '提.md', '乙', note['revision'], 'sha256:ev1')
    base = {'proposal_id': proposal['id'], 'text': '乙', 'base_revision': note['revision'], 'total': 1}
    monkeypatch.setattr(notes, '_evidence', lambda video_id: ('sha256:other', EVIDENCE[1]))
    assert client.post(url('/提.md/proposal/resolve'), json={**base, 'accepted': 1}).status_code == 409
    discarded = client.post(url('/提.md/proposal/resolve'), json={**base, 'accepted': 0})
    assert discarded.status_code == 200 and discarded.json()['text'] == '甲'
    assert client.get(url('/提.md/proposal')).json() is None


def test_resolve_rejects_unknown_citation_and_stale_base(client):
    note = notes.create(VID, title='提', text='甲')
    proposal = notes.propose(VID, '提.md', '乙', note['revision'], 'sha256:ev1')
    body = {'proposal_id': proposal['id'], 'text': '乙 [T9999]', 'base_revision': note['revision'], 'accepted': 1, 'total': 1}
    assert client.post(url('/提.md/proposal/resolve'), json=body).status_code == 400
    assert client.post(url('/提.md/proposal/resolve'), json={**body, 'text': '乙', 'base_revision': 'stale'}).status_code == 409
    assert client.delete(url('/提.md/proposal')).status_code == 200
    assert client.get(url('/提.md/proposal')).json() is None
