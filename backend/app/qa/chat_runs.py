"""A browser subscribes to a run; it does not own the run's lifetime."""
from __future__ import annotations

import copy
import logging
import queue
import threading
import time
import uuid

from .. import db
from . import codex_chat, conversations

log = logging.getLogger(__name__)
_guard = threading.RLock()
_active: dict[str, Run] = {}


def key(video_id, conversation_id):
    return f'chat_run:{video_id}:{conversation_id}'


def is_running(video_id=None):
    with _guard:
        return bool(_active) if video_id is None else video_id in _active


class Run:
    def __init__(self, video_id, question, options, run_id):
        self.video_id = video_id
        self.cancel = threading.Event()
        self.finished = threading.Event()
        self.events: queue.Queue | None = queue.Queue()
        self.saved_at = 0.0
        self.data = {
            'id': run_id, 'conversation_id': conversations.current(video_id),
            'question': question, 'options': options, 'status': 'running',
            'created_at': db.now_iso(), 'context': None, 'context_current': False,
            'compacting': False, 'preview': None, 'message': None, 'error': None,
            'pending': {'phase': 'start', 'expanded': None, 'text': '', 'corrected': False, 'activity': []},
        }

    def save(self):
        db.set_setting(key(self.video_id, self.data['conversation_id']), self.data)
        self.saved_at = time.monotonic()

    def detach(self):
        with _guard:
            self.events = None

    def publish(self, event):
        with _guard:
            kind = event['type']
            pending = self.data['pending']
            if kind == 'delta':
                pending['text'] += event['text']
                pending['phase'] = 'answer'
            elif kind == 'replace':
                pending.update(text=event['text'], corrected=True)
            elif kind == 'activity':
                pending['activity'].append(event['text'])
            elif kind == 'progress':
                pending['progress'] = event['text']
            elif kind == 'commentary':
                pending['commentary'] = (pending.get('commentary', '') if event.get('append') else '') + event['text']
            elif kind == 'note_changed':
                pending['note_updates'] = pending.get('note_updates', 0) + 1
            elif kind == 'interaction':
                pending['interaction'] = event['interaction']
            elif kind == 'phase':
                pending.update(phase=event['phase'], expanded=event.get('expanded'))
            elif kind == 'usage':
                pending['usage'] = event['usage']
            elif kind == 'context':
                self.data.update(context=event['context'], context_current=not event.get('previous', False))
            elif kind == 'compaction':
                self.data['compacting'] = event['active']
            elif kind == 'outline_preview':
                self.data['preview'] = event['preview']
            elif kind == 'done':
                self.data.update(status='done', message=event['message'], compacting=False)
            elif kind == 'error':
                self.data.update(status='error', error=event['error'], preview=None, compacting=False)
                pending['interaction'] = None
            # Keep only the latest snapshot per conversation, not an ever-growing event log.
            if kind in ('done', 'error', 'interaction') or time.monotonic() - self.saved_at >= 1:
                self.save()
            if self.events is not None:
                self.events.put(event)

    def produce(self, answer):
        iterator = None
        try:
            if self.cancel.is_set():
                raise RuntimeError('回答已停止')
            iterator = answer(self.video_id, self.data['question'], **self.data['options'])
            for event in iterator:
                if self.cancel.is_set():
                    raise RuntimeError('回答已停止')
                self.publish(event)
            if self.data['status'] == 'running':
                self.publish({'type': 'error', 'error': '回答意外结束，请检查后重试'})
        except Exception as error:
            log.exception('问答运行失败（%s）', self.video_id)
            self.publish({'type': 'error', 'error': str(error)})
        finally:
            try:
                if hasattr(iterator, 'close'):
                    iterator.close()
            finally:
                with _guard:
                    _active.pop(self.video_id, None)
                    self.finished.set()


def start(video_id, question, options, run_id, answer):
    lock = codex_chat.conversation_lock(video_id)
    if not lock.acquire(blocking=False):
        raise conversations.BusyError('这节课正在回答，请等待或停止当前回答')
    try:
        with _guard:
            if video_id in _active:
                raise conversations.BusyError('这节课正在回答，请等待或停止当前回答')
            run = Run(video_id, question, options, run_id or uuid.uuid4().hex)
            run.save()
            _active[video_id] = run
    finally:
        lock.release()
    try:
        threading.Thread(target=run.produce, args=(answer,), daemon=True, name=f'chat-{video_id}').start()
    except RuntimeError:
        with _guard:
            _active.pop(video_id, None)
        raise
    return run


def snapshot(video_id, conversation_id=None):
    conversation_id = conversation_id or conversations.current(video_id)
    with _guard:
        run = _active.get(video_id)
        if run and run.data['conversation_id'] == conversation_id:
            return copy.deepcopy(run.data)
        saved = db.get_setting(key(video_id, conversation_id))
        if saved and saved['status'] == 'running':
            saved.update(status='error', error='后端已重启，本轮回答已中断；可以查看已生成内容后手动重试。',
                         compacting=False, preview=None)
            db.set_setting(key(video_id, conversation_id), saved)
        return saved


def stop(video_id, run_id=None):
    with _guard:
        run = _active.get(video_id)
        if not run or (run_id and run.data['id'] != run_id):
            return
        run.cancel.set()
        codex_chat.stop(video_id)
        return run
