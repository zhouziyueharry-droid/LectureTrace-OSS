"""多段问答：旧库升级、隔离、清理和活动回答冲突，均不调用模型。"""
import json
import sqlite3

from fastapi.testclient import TestClient

from app import artifacts, config, db
from app.main import app
from app.api.videos import delete_video
from app.codex import settings as codex_settings
from app.qa import chat, codex_chat, conversations

VID = 'abc123def456'


def _video():
    db.execute("INSERT INTO videos (id,title,filename,created_at,status) VALUES (?,?,?,?,?)",
               (VID, '课', 'lesson.mp4', db.now_iso(), 'done'))


def test_old_database_keeps_codex_id_and_messages(tmp_settings):
    path = config.settings.db_path
    path.unlink()
    old_id = 'a' * 32
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE chat_messages (id INTEGER PRIMARY KEY, video_id TEXT, role TEXT, content TEXT, citations_json TEXT, created_at TEXT)')
        conn.execute('CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        conn.execute('INSERT INTO chat_messages (video_id,role,content,created_at) VALUES (?,?,?,?)',
                     (VID, 'user', '旧问题', '2026-09-01T00:00:00Z'))
        conn.execute('INSERT INTO app_settings VALUES (?,?)',
                     (f'codex_conversation:{VID}', json.dumps({'conversation_id': old_id, 'thread_id': 'old-thread'})))
    db.init_db()
    db.init_db()
    assert conversations.current(VID) == old_id
    assert db.fetch_one('SELECT conversation_id FROM chat_messages')['conversation_id'] == old_id
    assert codex_chat.state(VID)['thread_id'] == 'old-thread'
    assert conversations.listing(VID)['conversations'][0]['title'] == '旧问题'
    assert db.fetch_one('SELECT custom_title FROM chat_conversations WHERE id=?', (old_id,))['custom_title'] is None
    assert conversations.create(VID)['current_id'] != old_id


def test_rename_title_persists_without_changing_session_data(tmp_settings):
    _video()
    other = 'other12345678'
    db.execute("INSERT INTO videos (id,title,filename,created_at,status) VALUES (?,?,?,?,?)",
               (other, '另一课', 'other.mp4', db.now_iso(), 'done'))
    client = TestClient(app)
    first = conversations.current(VID)
    chat._insert(VID, 'user', '首个问题', None, version=None)
    db.set_setting(codex_chat._key(VID), {'conversation_id': first, 'thread_id': 'saved-thread'})
    before = conversations.listing(VID)['conversations'][0]['updated_at']

    path = f'/api/videos/{VID}/chat/conversations/{first}'
    response = client.patch(path, json={'title': '  我的复习主题  '})
    assert response.status_code == 200
    assert response.json()['conversations'][0]['title'] == '我的复习主题'
    assert conversations.listing(VID)['conversations'][0]['updated_at'] == before
    assert [m['content'] for m in chat.history(VID)] == ['首个问题']
    assert codex_chat.state(VID)['thread_id'] == 'saved-thread'
    assert client.get(f'/api/videos/{VID}/chat/conversations').json()['conversations'][0]['title'] == '我的复习主题'
    assert client.patch(f'/api/videos/{other}/chat/conversations/{first}', json={'title': '越界'}).status_code == 404
    assert client.patch(path, json={'title': '   '}).status_code == 400
    assert client.patch(path, json={'title': '长' * 81}).status_code == 400

    second = conversations.create(VID)['current_id']
    assert conversations.listing(VID)['conversations'][0]['title'] is None
    conversations.select(VID, first)
    assert conversations.listing(VID)['conversations'][1]['id'] == first
    assert conversations.listing(VID)['conversations'][1]['title'] == '我的复习主题'
    assert second != first


def test_existing_session_table_gains_title_column_without_losing_rows(tmp_settings):
    path = config.settings.db_path
    path.unlink()
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE chat_conversations (id TEXT PRIMARY KEY, video_id TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)')
        conn.execute('CREATE TABLE chat_messages (id INTEGER PRIMARY KEY, video_id TEXT, conversation_id TEXT, role TEXT, content TEXT, citations_json TEXT, created_at TEXT)')
        conn.execute('INSERT INTO chat_conversations VALUES (?,?,?,?)', ('old-session', VID, '2026-09-01', '2026-09-01'))
        conn.execute('INSERT INTO chat_messages (video_id,conversation_id,role,content,created_at) VALUES (?,?,?,?,?)',
                     (VID, 'old-session', 'user', '迁移前的问题', '2026-09-01'))
    db.init_db()
    assert conversations.listing(VID)['conversations'][0]['title'] == '迁移前的问题'
    conversations.rename(VID, 'old-session', ' 复习重点 ')
    db.init_db()
    assert conversations.listing(VID)['conversations'][0]['title'] == '复习重点'
    assert conversations.create(VID)['current_id'] != 'old-session'


def test_create_switch_delete_keeps_other_session_and_usage(tmp_settings, tmp_path):
    _video()
    root = tmp_path / 'workspace'
    db.set_setting('codex_qa_settings', {'workspace_root': str(root)})
    first = conversations.current(VID)
    chat._insert(VID, 'user', '旧问题', None, version=None)
    db.set_setting(codex_chat._key(VID), {'conversation_id': first, 'thread_id': 'first-thread'})
    note = root / VID / first / 'notes' / 'memo.md'
    note.parent.mkdir(parents=True)
    note.write_text('old', encoding='utf-8')
    log = artifacts.video_dir(VID) / 'codex_reads' / f'{first}.json'
    log.parent.mkdir(parents=True)
    log.write_text('{}', encoding='utf-8')
    db.execute('INSERT INTO codex_usage VALUES (NULL,?,?,?,?,?)', (VID, first, 'turn', '{}', db.now_iso()))

    second = conversations.create(VID)['current_id']
    assert second != first and chat.history(VID) == []
    chat._insert(VID, 'user', '新问题', None, version=None)
    db.set_setting(codex_chat._key(VID), {'conversation_id': second, 'thread_id': 'second-thread'})
    assert [r['title'] for r in conversations.listing(VID)['conversations']] == ['新问题', '旧问题']
    conversations.select(VID, first)
    assert codex_chat.state(VID)['thread_id'] == 'first-thread'
    assert [m['content'] for m in chat.history(VID)] == ['旧问题']
    conversations.select(VID, second)
    assert codex_chat.state(VID)['thread_id'] == 'second-thread'
    assert note.read_text(encoding='utf-8') == 'old'

    conversations.delete_current(VID)
    assert conversations.current(VID) == first
    assert note.exists() and log.exists()
    assert [m['content'] for m in chat.history(VID)] == ['旧问题']
    assert db.fetch_one('SELECT COUNT(*) AS n FROM codex_usage')['n'] == 1
    conversations.delete_current(VID)
    assert not note.exists() and not log.exists()
    assert len(conversations.listing(VID)['conversations']) == 1
    assert chat.history(VID) == []


def test_create_reuses_blank_session_instead_of_piling_up(tmp_settings):
    _video()
    client = TestClient(app)
    first = conversations.current(VID)
    chat._insert(VID, 'user', '有内容的对话', None, version=None)
    path = f'/api/videos/{VID}/chat/conversations'
    second = client.post(path).json()['current_id']
    assert second != first
    for _ in range(3):  # 连点：当前已是空对话，不再新建
        assert client.post(path).json()['current_id'] == second
    assert len(conversations.listing(VID)['conversations']) == 2
    # 切回有内容的对话后再点新建，回到那段已有的空对话，而不是再建一段
    conversations.select(VID, first)
    assert client.post(path).json()['current_id'] == second
    assert len(conversations.listing(VID)['conversations']) == 2
    # 改过名的空对话算有主题，不被复用
    conversations.rename(VID, second, '复习计划')
    assert client.post(path).json()['current_id'] not in (first, second)
    assert len(conversations.listing(VID)['conversations']) == 3


def test_api_rejects_session_changes_while_answer_lock_held(tmp_settings):
    _video()
    client = TestClient(app)
    first = client.get(f'/api/videos/{VID}/chat/conversations').json()['current_id']
    lock = codex_chat.conversation_lock(VID)
    lock.acquire()
    try:
        assert client.post(f'/api/videos/{VID}/chat/conversations').status_code == 409
        assert client.put(f'/api/videos/{VID}/chat/conversations/{first}').status_code == 409
        assert client.patch(f'/api/videos/{VID}/chat/conversations/{first}', json={'title': '锁内改名'}).status_code == 409
        assert client.delete(f'/api/videos/{VID}/chat').status_code == 409
    finally:
        lock.release()
    assert client.post(f'/api/videos/{VID}/chat/conversations').status_code == 200


def test_workspace_root_change_copies_every_session_note(tmp_settings, tmp_path):
    _video()
    source, target = tmp_path / 'before', tmp_path / 'after'
    db.set_setting('codex_qa_settings', {'workspace_root': str(source)})
    first = conversations.current(VID)
    chat._insert(VID, 'user', '先问一句', None, version=None)  # 空对话会被复用，需先有内容才会另起一段
    second = conversations.create(VID)['current_id']
    for cid in (first, second):
        note = source / VID / cid / 'notes' / 'memo.md'
        note.parent.mkdir(parents=True)
        note.write_text(cid, encoding='utf-8')
    db.set_setting(codex_chat._key(VID), {'conversation_id': second, 'thread_id': 'old'})
    codex_settings.update({'workspace_root': str(target)})
    assert [(target / VID / cid / 'notes' / 'memo.md').read_text(encoding='utf-8') for cid in (first, second)] == [first, second]
    assert 'thread_id' not in codex_chat.state(VID)
    conversations.select(VID, first)
    assert 'thread_id' not in codex_chat.state(VID)


def test_delete_video_removes_all_session_notes(tmp_settings, tmp_path):
    _video()
    root = tmp_path / 'workspace'
    db.set_setting('codex_qa_settings', {'workspace_root': str(root)})
    first = conversations.current(VID)
    chat._insert(VID, 'user', first, None, version=None, conversation_id=first)  # 空对话会被复用，需先有内容
    second = conversations.create(VID)['current_id']
    for cid in (first, second):
        note = root / VID / cid / 'notes' / 'memo.md'
        note.parent.mkdir(parents=True)
        note.write_text('note', encoding='utf-8')
        if cid == second:
            chat._insert(VID, 'user', cid, None, version=None, conversation_id=cid)
    assert delete_video(VID) == {'ok': True}
    assert db.fetch_one('SELECT COUNT(*) AS n FROM chat_conversations')['n'] == 0
    assert db.fetch_one('SELECT COUNT(*) AS n FROM chat_messages')['n'] == 0
    assert not (root / VID / first / 'notes').exists()
    assert not (root / VID / second / 'notes').exists()
