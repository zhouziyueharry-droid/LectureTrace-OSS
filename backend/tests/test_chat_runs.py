"""No model calls: a disconnected subscriber must not cancel the producer."""
import asyncio
import threading
import time

import pytest

from app import db
from app.qa import conversations

VID = 'abc123def456'


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.01)
    raise AssertionError('run did not reach expected state')


def test_disconnect_resume_and_stop(tmp_settings, monkeypatch):
    from app.qa import chat_runs, codex_chat
    db.execute('INSERT INTO videos (id,title,filename,status,created_at) VALUES (?,?,?,?,?)',
               (VID, 'fixture', 'fixture.mp4', 'done', db.now_iso()))
    cid = conversations.current(VID)
    release = threading.Event()
    entered = threading.Event()
    calls = []
    stops = []
    monkeypatch.setattr(codex_chat, 'stop', lambda vid: stops.append(vid))

    def answer(*args, **kwargs):
        calls.append(args)
        yield {'type': 'activity', 'text': '正在核对课件'}
        yield {'type': 'progress', 'text': '查看 PDF · 第 35 页'}
        yield {'type': 'commentary', 'text': '先核对', 'append': False}
        yield {'type': 'commentary', 'text': '公式', 'append': True}
        yield {'type': 'note_changed'}
        yield {'type': 'delta', 'text': '已生成的部分'}
        entered.set()
        assert release.wait(3)
        yield {'type': 'done', 'message': {'id': 123, 'role': 'assistant', 'content': '完整回答'}}

    from app.api.chat import chat_ask, AskBody
    from app.qa import chat
    monkeypatch.setattr(chat, 'ask', answer)

    async def disconnect():
        response = await chat_ask(VID, AskBody(question='问题', run_id='run-test'))
        assert 'activity' in (await anext(response.body_iterator))['data']
        await response.body_iterator.aclose()

    asyncio.run(disconnect())
    try:
        assert entered.wait(2)
        pending = chat_runs.snapshot(VID, cid)['pending']
        assert pending['progress'] == '查看 PDF · 第 35 页'
        assert pending['commentary'] == '先核对公式' and pending['note_updates'] == 1
        snapshot = chat_runs.snapshot(VID, cid)
        assert snapshot['status'] == 'running'
        assert snapshot['pending']['text'] == '已生成的部分'
        assert snapshot['pending']['activity'] == ['正在核对课件']
        assert not stops and len(calls) == 1
        with pytest.raises(conversations.BusyError):
            chat_runs.start(VID, '问题', {}, 'run-second', answer)
        with pytest.raises(conversations.BusyError):
            conversations.create(VID)
    finally:
        release.set()
        wait_for(lambda: not chat_runs.is_running(VID))
    assert chat_runs.snapshot(VID, cid)['message']['id'] == 123
    assert chat_runs.snapshot(VID, 'another-conversation') is None
    assert chat_runs.snapshot(VID, cid)['status'] == 'done'

    release.clear(); entered.clear()
    run = chat_runs.start(VID, '第二个问题', {}, 'run-stop', answer)
    try:
        assert entered.wait(2)
        chat_runs.stop(VID, 'stale-run')
        assert not stops
        chat_runs.stop(VID, 'run-stop')
        assert stops == [VID]
    finally:
        release.set()
        wait_for(lambda: not chat_runs.is_running(VID))
    snapshot = chat_runs.snapshot(VID, cid)
    assert snapshot['status'] == 'error' and '停止' in snapshot['error']
    assert snapshot['pending']['text'] == '已生成的部分'
    # A process restart cannot resume a dead thread or accidentally spend money.
    snapshot['status'] = 'running'
    db.set_setting(chat_runs.key(VID, cid), snapshot)
    assert '后端' in chat_runs.snapshot(VID, cid)['error']
