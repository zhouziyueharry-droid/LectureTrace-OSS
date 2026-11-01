"""每视频多段问答的选择与清理。变更须持有视频级问答锁。"""
from __future__ import annotations

import json
import uuid

from .. import db


class BusyError(Exception):
    pass


class MissingError(Exception):
    pass


class InvalidTitleError(Exception):
    pass


def current(video_id: str) -> str:
    selected = db.get_setting(f'chat_current:{video_id}')
    if selected and db.fetch_one('SELECT 1 FROM chat_conversations WHERE video_id=? AND id=?', (video_id, selected)):
        return selected
    # 两个首次读取可能并发；只在缺少选中值时使用 SQLite 写事务初始化一次。
    with db.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        saved = conn.execute('SELECT value FROM app_settings WHERE key=?', (f'chat_current:{video_id}',)).fetchone()
        selected = json.loads(saved['value']) if saved else None
        if selected and conn.execute('SELECT 1 FROM chat_conversations WHERE video_id=? AND id=?', (video_id, selected)).fetchone():
            return selected
        row = conn.execute('SELECT id FROM chat_conversations WHERE video_id=? ORDER BY updated_at DESC, rowid DESC LIMIT 1', (video_id,)).fetchone()
        if row:
            selected = row['id']
        else:
            old = conn.execute('SELECT value FROM app_settings WHERE key=?', (f'codex_conversation:{video_id}',)).fetchone()
            state = json.loads(old['value']) if old else {}
            selected = state.get('conversation_id') or uuid.uuid4().hex
            stamp = db.now_iso()
            conn.execute('INSERT INTO chat_conversations (id,video_id,created_at,updated_at) VALUES (?,?,?,?)',
                         (selected, video_id, stamp, stamp))
            conn.execute('UPDATE chat_messages SET conversation_id=? WHERE video_id=? AND conversation_id IS NULL', (selected, video_id))
        conn.execute('INSERT OR REPLACE INTO app_settings VALUES (?,?)', (f'chat_current:{video_id}', json.dumps(selected)))
        old = conn.execute('SELECT value FROM app_settings WHERE key=?', (f'codex_conversation:{video_id}',)).fetchone()
        state = json.loads(old['value']) if old else {}
        if state.get('conversation_id') != selected:
            conn.execute('INSERT OR REPLACE INTO app_settings VALUES (?,?)',
                         (f'codex_conversation:{video_id}', json.dumps({'conversation_id': selected})))
        return selected


def listing(video_id: str) -> dict:
    selected = current(video_id)
    rows = db.fetch_all('''SELECT c.id, c.created_at, c.updated_at,
        COALESCE(c.custom_title,
        (SELECT content FROM chat_messages WHERE video_id=c.video_id AND conversation_id=c.id
         AND role='user' ORDER BY id LIMIT 1)) AS title
        FROM chat_conversations c WHERE c.video_id=? ORDER BY c.updated_at DESC, c.rowid DESC''', (video_id,))
    return {'current_id': selected, 'conversations': rows}


def _locked(video_id: str, action):
    from . import codex_chat, chat_runs
    lock = codex_chat.conversation_lock(video_id)
    if not lock.acquire(blocking=False):
        raise BusyError('这节课正在回答，请等待或停止当前回答')
    try:
        if chat_runs.is_running(video_id):
            raise BusyError('这节课正在回答，请等待或停止当前回答')
        return action()
    finally:
        lock.release()


def create(video_id: str) -> dict:
    def action():
        from . import codex_chat
        selected = current(video_id)
        # 已有没提过问题、也没改过名的空对话就复用，避免连点「新建」堆出一串空对话
        blank = db.fetch_one('''SELECT c.id FROM chat_conversations c WHERE c.video_id=? AND c.custom_title IS NULL
            AND NOT EXISTS (SELECT 1 FROM chat_messages m WHERE m.video_id=c.video_id AND m.conversation_id=c.id)
            ORDER BY (c.id=?) DESC, c.updated_at DESC LIMIT 1''', (video_id, selected))
        if blank:
            if blank['id'] != selected:
                codex_chat.switch_locked(video_id, blank['id'])
            return listing(video_id)
        cid = uuid.uuid4().hex
        stamp = db.now_iso()
        db.execute('INSERT INTO chat_conversations (id,video_id,created_at,updated_at) VALUES (?,?,?,?)',
                   (cid, video_id, stamp, stamp))
        codex_chat.switch_locked(video_id, cid)
        return listing(video_id)
    return _locked(video_id, action)


def select(video_id: str, conversation_id: str) -> dict:
    def action():
        from . import codex_chat
        if not db.fetch_one('SELECT 1 FROM chat_conversations WHERE video_id=? AND id=?', (video_id, conversation_id)):
            raise MissingError('对话不存在')
        current(video_id)
        codex_chat.switch_locked(video_id, conversation_id)
        return listing(video_id)
    return _locked(video_id, action)


def rename(video_id: str, conversation_id: str, title: str) -> dict:
    title = title.strip()
    if not 1 <= len(title) <= 80:
        raise InvalidTitleError('标题须为 1–80 个字符')

    def action():
        with db.connect() as conn:
            changed = conn.execute(
                'UPDATE chat_conversations SET custom_title=? WHERE video_id=? AND id=?',
                (title, video_id, conversation_id),
            ).rowcount
        if not changed:
            raise MissingError('对话不存在')
        return listing(video_id)
    return _locked(video_id, action)


def delete_current(video_id: str) -> dict:
    return _locked(video_id, lambda: delete_current_locked(video_id))


def delete_current_locked(video_id: str) -> dict:
    from . import codex_chat
    cid = current(video_id)
    db.execute('DELETE FROM app_settings WHERE key=?', (f'chat_run:{video_id}:{cid}',))
    codex_chat.clear_saved_locked(video_id, cid)
    db.execute('DELETE FROM chat_messages WHERE video_id=? AND conversation_id=?', (video_id, cid))
    db.execute('DELETE FROM chat_conversations WHERE video_id=? AND id=?', (video_id, cid))
    db.execute('DELETE FROM app_settings WHERE key=?', (codex_chat._key(video_id),))
    remaining = db.fetch_one('SELECT id FROM chat_conversations WHERE video_id=? ORDER BY updated_at DESC, rowid DESC LIMIT 1', (video_id,))
    if remaining:
        next_id = remaining['id']
        db.set_setting(f'chat_current:{video_id}', next_id)
        codex_chat.load_locked(video_id, next_id)
    else:
        db.execute('DELETE FROM app_settings WHERE key=?', (f'chat_current:{video_id}',))
        current(video_id)
    return listing(video_id)


def delete_video_locked(video_id: str) -> None:
    from . import codex_chat
    if db.get_setting(codex_chat._key(video_id)):
        current(video_id)
    for row in db.fetch_all('SELECT id FROM chat_conversations WHERE video_id=?', (video_id,)):
        codex_chat.clear_saved_locked(video_id, row['id'])
        db.execute('DELETE FROM app_settings WHERE key=?', (f"chat_run:{video_id}:{row['id']}",))
    db.execute('DELETE FROM chat_messages WHERE video_id=?', (video_id,))
    db.execute('DELETE FROM chat_conversations WHERE video_id=?', (video_id,))
    for key in (f'chat_current:{video_id}', codex_chat._key(video_id)):
        db.execute('DELETE FROM app_settings WHERE key=?', (key,))
