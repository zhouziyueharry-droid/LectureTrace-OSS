"""Codex 工具循环与持久会话集成；模型事件全部模拟。"""
import json
import queue
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import artifacts, db
from app.codex import runtime, settings
from app.qa import chat, codex_chat, outline_edits

VID = 'abc123def456'
EV = {'pages': [], 'sections': [{'id': 'N001', 'page': None, 'jump': 0, 'speech_start': 0,
                               'speech_end': 10, 't_ids': ['T0001'], 'spoken_summary': ''}],
      'speech': [{'id': 'T0001', 'section': 'N001', 'start': 0, 'end': 10, 'jump': 0, 'text': 'A batch is normalized.'}]}


class FakeRuntime:
    def __init__(self, *, answer='Batch normalization. [T0001]', read=True, correction=None, account='chatgpt'):
        self.answer, self.read, self.correction, self.account = answer, read, correction, account
        self.requests, self.responses = [], []
        self.events = queue.Queue()
        self.runs = self.starts = 0
        self._closed = False

    def request(self, method, params=None, **kwargs):
        self.requests.append((method, params))
        if method == 'account/read':
            return {'account': {'type': self.account}}
        if method == 'account/rateLimits/read':
            return {'rateLimits': None}
        if method == 'thread/start':
            self.starts += 1
            return {'thread': {'id': 'thread', 'environments': [{'environmentId': 'local'}]}, 'approvalPolicy': 'on-request',
                    'sandbox': {'type': 'workspaceWrite', 'networkAccess': True}, 'modelProvider': 'openai',
                    'approvalsReviewer': 'user', 'instructionSources': []}
        if method == 'thread/resume':
            assert params['excludeTurns'] is True
            return {'thread': {'id': 'thread', 'environments': [{'environmentId': 'local'}]}, 'approvalPolicy': 'on-request',
                    'sandbox': {'type': 'workspaceWrite', 'networkAccess': True}, 'modelProvider': 'openai',
                    'approvalsReviewer': 'user', 'instructionSources': []}
        if method == 'turn/start':
            self.runs += 1
            tid = f'turn{self.runs}'
            if self.read and self.runs == 1:
                self.events.put({'id': 100, 'method': 'item/tool/call', 'params': {
                    'threadId': 'thread', 'turnId': tid, 'tool': 'read_evidence', 'arguments': {'ids': ['T0001']}}})
            self.events.put({'method': 'thread/tokenUsage/updated', 'params': {'threadId': 'thread', 'turnId': tid,
                'tokenUsage': {'modelContextWindow': 1000,
                              'last': {'inputTokens': 100, 'outputTokens': 20, 'totalTokens': 120,
                                       'reasoningOutputTokens': 10, 'cachedInputTokens': 40},
                              'total': {'inputTokens': 100 * self.runs, 'outputTokens': 20 * self.runs,
                                       'reasoningOutputTokens': 10 * self.runs, 'cachedInputTokens': 40 * self.runs}}}})
            for phase, text in [('commentary', '正在核对'), ('final_answer', self.answer if self.runs == 1 else self.correction or self.answer)]:
                self.events.put({'method': 'item/completed', 'params': {'threadId': 'thread', 'turnId': tid,
                    'item': {'id': phase, 'type': 'agentMessage', 'phase': phase, 'text': text}}})
            self.events.put({'method': 'turn/completed', 'params': {'threadId': 'thread', 'turn': {'id': tid, 'status': 'completed'}}})
            return {'turn': {'id': tid}}
        return {}

    def subscribe(self, thread):
        return self.events

    def unsubscribe(self, thread):
        pass

    def respond(self, rid, result):
        self.responses.append(result)


@pytest.fixture
def setup_course(tmp_settings, tmp_path):
    artifacts.write_artifact(VID, 'evidence.json', EV, {})
    settings.update({'provider': 'codex', 'workspace_root': str(tmp_path / 'workspaces')})


def test_agent_loop_reads_evidence_and_separates_commentary(setup_course, monkeypatch):
    client = FakeRuntime()
    monkeypatch.setattr(runtime, 'get_runtime', lambda: client)
    events = list(chat.ask(VID, 'What is normalized?'))
    done = next(e['message'] for e in events if e['type'] == 'done')
    assert done['content'] == client.answer
    assert done['citations'] == [{'id': 'T0001', 'start': 0.0}]
    assert not chat.history(VID)[-1]['stale']
    assert client.responses[0]['success']
    assert all('environments' not in params for method, params in client.requests if method == 'turn/start')
    assert db.fetch_one('SELECT usage_json FROM codex_usage')


def test_unsupported_resume_uses_native_full_history_fork_only(setup_course, monkeypatch):
    client = FakeRuntime()
    monkeypatch.setattr(runtime, 'get_runtime', lambda: client)
    assert any(e['type'] == 'done' for e in chat.ask(VID, 'First request'))
    original = client.request
    def request(method, params=None, **kwargs):
        if method == 'thread/resume':
            raise runtime.CodexError('list_turns is not supported yet', code=-32601)
        if method == 'thread/fork':
            assert params['threadId'] == 'thread' and params['deferGoalContinuation']
            return original('thread/start', params)
        return original(method, params, **kwargs)
    monkeypatch.setattr(client, 'request', request)
    assert any(e['type'] == 'done' for e in chat.ask(VID, 'Second request'))
    assert codex_chat.state(VID)['recovered_from_thread'] == 'thread'
    assert 'First request' not in next(p['input'][0]['text'] for m,p in reversed(client.requests) if m == 'turn/start')


def test_rebuilt_thread_places_current_request_after_history(setup_course, monkeypatch):
    client = FakeRuntime(answer='Completed.', read=False)
    monkeypatch.setattr(runtime, 'get_runtime', lambda: client)
    assert any(e['type'] == 'done' for e in chat.ask(VID, 'First request'))
    changed = json.loads(json.dumps(EV))
    changed['speech'][0]['text'] = 'Updated evidence.'
    artifacts.write_artifact(VID, 'evidence.json', changed, {})
    assert any(e['type'] == 'done' for e in chat.ask(VID, 'Second request'))
    prompt = [p['input'][0]['text'] for m, p in client.requests if m == 'turn/start'][-1]
    assert prompt.index('First request') < prompt.index('学生问题：Second request')


def test_live_context_before_answer_and_refresh_invalidates_old_settings(setup_course, monkeypatch):
    client = FakeRuntime()
    monkeypatch.setattr(runtime, 'get_runtime', lambda: client)
    events = list(chat.ask(VID, 'First'))
    snapshot = next(e['context'] for e in events if e['type'] == 'context' and e['context'])
    assert snapshot['last']['totalTokens'] == 120
    assert snapshot['context_window'] == 1000
    assert snapshot['usage']['input_tokens'] == 100
    assert next(i for i, e in enumerate(events) if e['type'] == 'context' and e['context']) < next(i for i, e in enumerate(events) if e['type'] == 'delta')
    assert codex_chat.context_snapshot(VID) == snapshot
    second = list(chat.ask(VID, 'Second'))
    assert next(e for e in second if e['type'] == 'context')['previous']
    snapshot = next(e['context'] for e in reversed(second) if e['type'] == 'context')
    assert snapshot['last']['totalTokens'] == 120
    assert snapshot['usage']['input_tokens'] == 100  # not the lifetime total 200
    settings.update({'model': 'other-model'})
    assert codex_chat.context_snapshot(VID) == snapshot
    codex_chat.reset(VID)
    assert codex_chat.context_snapshot(VID) is None


def test_unread_citation_repaired_once_and_never_uses_api(setup_course, monkeypatch):
    client = FakeRuntime(read=False, correction='缺少正文依据，位置在 [定位:N001]。')
    monkeypatch.setattr(runtime, 'get_runtime', lambda: client)
    events = list(chat.ask(VID, 'Explain'))
    done = next(e['message'] for e in events if e['type'] == 'done')
    assert client.runs == 2
    assert done['citations'] == [{'id': '定位:N001', 'start': 0.0}]
    assert next(e['usage'] for e in events if e['type'] == 'usage')['input_tokens'] == 200
    assert [e['text'] for e in events if e['type'] == 'delta'] == [client.answer]
    snapshots = [e['context'] for e in events if e['type'] == 'context' and e['context']]
    assert [s['usage']['input_tokens'] for s in snapshots] == [100, 200]
    assert snapshots[-1]['last']['totalTokens'] == 120


def test_context_compaction_and_unknown_counters_do_not_break_answer(setup_course, monkeypatch):
    client = FakeRuntime()
    request = client.request
    def with_compaction(method, params=None, **kwargs):
        result = request(method, params, **kwargs)
        if method == 'turn/start':
            queued = []
            while not client.events.empty():
                message = client.events.get_nowait()
                if message['method'] == 'thread/tokenUsage/updated':
                    for event in ('item/started', 'item/completed'):
                        queued.append({'method': event, 'params': {'threadId': 'thread', 'turnId': 'turn1',
                                                                  'item': {'id': 'compact', 'type': 'contextCompaction'}}})
                    message['params']['tokenUsage'].update(last={'inputTokens': False}, modelContextWindow=None)
                queued.append(message)
            for message in queued:
                client.events.put(message)
        return result
    client.request = with_compaction
    monkeypatch.setattr(runtime, 'get_runtime', lambda: client)
    events = list(chat.ask(VID, 'Explain'))
    assert [e['active'] for e in events if e['type'] == 'compaction'] == [True, False]
    snapshot = next(e['context'] for e in events if e['type'] == 'context' and e['context'])
    assert snapshot['context_window'] is None and all(value is None for value in snapshot['last'].values())
    assert events[-1]['type'] == 'done'


def test_unchanged_timestamp_changed_evidence_invalidates_history(setup_course, monkeypatch):
    client = FakeRuntime()
    monkeypatch.setattr(runtime, 'get_runtime', lambda: client)
    list(chat.ask(VID, 'Explain'))
    path = artifacts.video_dir(VID) / 'evidence.json'
    obj = artifacts.read_json(path)
    obj['data']['speech'][0]['text'] = 'Different evidence.'
    artifacts.write_json(path, obj)
    assert chat.history(VID)[-1]['stale']
    assert chat.history(VID)[-1]['citations'] == []


def test_api_key_account_is_rejected_without_starting_turn(setup_course, monkeypatch):
    client = FakeRuntime(account='apiKey')
    monkeypatch.setattr(runtime, 'get_runtime', lambda: client)
    assert list(chat.ask(VID, 'Explain'))[-1]['type'] == 'error'
    assert client.runs == 0


def test_exhausted_included_quota_never_starts_a_model_turn(setup_course, monkeypatch):
    client = FakeRuntime()
    request = client.request

    def exhausted(method, params=None, **kwargs):
        if method == 'account/rateLimits/read':
            return {'ordinaryUsageAllowed': False, 'rateLimits': {'credits': {'hasCredits': True}}}
        return request(method, params, **kwargs)

    monkeypatch.setattr(client, 'request', exhausted)
    monkeypatch.setattr(runtime, 'get_runtime', lambda: client)
    events = list(chat.ask(VID, 'Explain'))
    assert events[-1]['type'] == 'error' and '额度' in events[-1]['error']
    assert client.runs == client.starts == 0
    assert [m['content'] for m in chat.history(VID)] == ['Explain']


def test_citation_repair_checks_quota_again_before_starting_model(setup_course, monkeypatch):
    client = FakeRuntime(read=False)
    request = client.request
    allowance = iter([True, True, False])  # initialization, answer, citation repair

    def exhaustion_during_answer(method, params=None, **kwargs):
        if method == 'account/rateLimits/read':
            return {'ordinaryUsageAllowed': next(allowance)}
        return request(method, params, **kwargs)

    monkeypatch.setattr(client, 'request', exhaustion_during_answer)
    monkeypatch.setattr(runtime, 'get_runtime', lambda: client)
    events = list(chat.ask(VID, 'Explain'))
    assert events[-1]['type'] == 'error' and '额度' in events[-1]['error']
    assert client.runs == 1
    assert not any(e['type'] == 'done' for e in events)


def test_clear_changes_conversation_and_removes_notes(setup_course, monkeypatch):
    client = FakeRuntime()
    monkeypatch.setattr(runtime, 'get_runtime', lambda: client)
    list(chat.ask(VID, 'Explain'))
    before = codex_chat.state(VID)
    from app.qa.workspace import CourseWorkspace
    workspace = CourseWorkspace(VID, before['conversation_id'], settings.get())
    workspace.begin_turn('note')
    assert workspace.call('write_note', {'path': 'notes/a.md', 'text': 'memo'})['success']
    chat.clear(VID)
    assert codex_chat.state(VID)['conversation_id'] != before['conversation_id']
    assert not (workspace.root / 'notes' / 'a.md').exists()
    assert chat.history(VID) == []


@pytest.mark.parametrize('stop_method', ['account/read', 'account/rateLimits/read'])
def test_stop_during_initialization_never_starts_model(setup_course, monkeypatch, stop_method):
    client = FakeRuntime()
    request = client.request
    quota_reads = 0

    def stopping_request(method, params=None, **kwargs):
        nonlocal quota_reads
        if method == 'account/rateLimits/read':
            quota_reads += 1
        if method == stop_method and (stop_method == 'account/read' or quota_reads == 2):
            codex_chat.stop(VID)
        return request(method, params, **kwargs)

    monkeypatch.setattr(client, 'request', stopping_request)
    monkeypatch.setattr(runtime, 'get_runtime', lambda: client)
    assert list(chat.ask(VID, 'Explain'))[-1]['type'] == 'error'
    assert client.runs == 0


def test_thread_without_native_environment_is_rejected(setup_course, monkeypatch):
    client = FakeRuntime()
    request = client.request

    def unsafe_request(method, params=None, **kwargs):
        result = request(method, params, **kwargs)
        if method == 'thread/start':
            result['thread']['environments'] = []
        return result

    monkeypatch.setattr(client, 'request', unsafe_request)
    monkeypatch.setattr(runtime, 'get_runtime', lambda: client)
    assert list(chat.ask(VID, 'Explain'))[-1]['type'] == 'error'
    assert client.runs == 0


def test_followup_resumes_thread_and_uses_prior_read_evidence(setup_course, monkeypatch):
    client = FakeRuntime()
    monkeypatch.setattr(runtime, 'get_runtime', lambda: client)
    list(chat.ask(VID, 'Explain'))
    second = list(chat.ask(VID, 'Explain again'))
    assert second[-1]['type'] == 'done'
    assert client.starts == 1
    assert any(method == 'thread/resume' for method, _ in client.requests)
    assert next(e['usage'] for e in second if e['type'] == 'usage')['input_tokens'] == 100


def test_failed_initialization_preserves_corrections_and_attachment(setup_course, monkeypatch):
    client = FakeRuntime()
    request = client.request
    def unavailable(method, params=None, **kwargs):
        if method == 'account/read':
            raise runtime.CodexError('connection lost')
        return request(method, params, **kwargs)
    client.request = unavailable
    monkeypatch.setattr(runtime, 'get_runtime', lambda: client)
    target = {'name': 'lecture.md', 'revision': 'r1'}
    assert list(chat.ask(VID, 'PDF为主，老师辅助', note_target=target))[-1]['type'] == 'error'
    assert list(chat.ask(VID, '只在必要时给时间戳'))[-1]['type'] == 'error'
    client.request = request
    assert list(chat.ask(VID, '再试一次'))[-1]['type'] == 'done'
    prompt = next(p['input'][0]['text'] for m, p in client.requests if m == 'turn/start')
    assert 'PDF为主，老师辅助' in prompt and '只在必要时给时间戳' in prompt
    assert 'lecture.md' in prompt and 'citations' not in prompt
    assert [m['content'] for m in chat.history(VID) if m['role'] == 'user'] == [
        'PDF为主，老师辅助', '只在必要时给时间戳', '再试一次']


def test_failed_turn_and_model_change_resume_original_thread(setup_course, monkeypatch):
    client = FakeRuntime()
    request = client.request
    def interrupted(method, params=None, **kwargs):
        result = request(method, params, **kwargs)
        if method == 'turn/start' and client.runs == 1:
            while not client.events.empty():
                client.events.get_nowait()
            client.events.put({'method': 'turn/completed', 'params': {
                'threadId': 'thread', 'turn': {'id': 'turn1', 'status': 'failed',
                                            'error': {'message': 'interrupted'}}}})
        return result
    client.request = interrupted
    client.answer = '继续完成。'
    monkeypatch.setattr(runtime, 'get_runtime', lambda: client)
    assert list(chat.ask(VID, 'PDF为主'))[-1]['type'] == 'error'
    assert codex_chat.state(VID)['thread_id'] == 'thread'
    settings.update({'model': 'other-model', 'effort': 'high'})
    assert list(chat.ask(VID, '继续'))[-1]['type'] == 'done'
    assert client.starts == 1
    assert [p for m, p in client.requests if m == 'turn/start'][-1]['model'] == 'other-model'


def test_model_selection_is_conversation_local_and_used_by_next_turn(setup_course, monkeypatch):
    from app.qa import conversations
    client = FakeRuntime(answer='已完成。')
    monkeypatch.setattr(runtime, 'get_runtime', lambda: client)
    monkeypatch.setattr(runtime, 'model_choices', lambda: [
        {'id': 'model-a', 'efforts': ['medium', 'high'], 'default_effort': 'medium'}])
    list(chat.ask(VID, '开始'))
    first = conversations.current(VID)
    original_thread = codex_chat.state(VID)['thread_id']
    codex_chat.select_model(VID, 'model-a', 'high')
    assert codex_chat.state(VID)['thread_id'] == original_thread
    assert settings.get()['model'] == 'gpt-6-sol'
    with pytest.raises(ValueError):
        codex_chat.select_model(VID, 'model-a', 'unsupported')
    list(chat.ask(VID, '继续'))
    sent = [p for m, p in client.requests if m == 'turn/start'][-1]
    assert (sent['model'], sent['effort']) == ('model-a', 'high')
    conversations.create(VID)
    assert codex_chat.model_selection(VID)['model'] == 'gpt-6-sol'
    conversations.select(VID, first)
    assert codex_chat.model_selection(VID) == {'model': 'model-a', 'effort': 'high'}


def test_note_target_reaches_prompt_and_stale_target_never_starts_turn(setup_course, monkeypatch):
    from app.qa import notes
    client = FakeRuntime()
    monkeypatch.setattr(runtime, 'get_runtime', lambda: client)
    original = notes.save(VID, 'target.md', 'original', None)
    target = {'name': original['name'], 'revision': original['revision']}
    events = list(chat.ask(VID, '补充笔记', note_target=target))
    assert events[-1]['type'] == 'done'
    prompt = next(params['input'][0]['text'] for method, params in client.requests if method == 'turn/start')
    assert 'target.md' in prompt and original['revision'] in prompt
    notes.save(VID, 'target.md', 'human update', original['revision'])
    events = list(chat.ask(VID, '补充笔记', note_target=target))
    assert any(e.get('type') == 'error' and '已更新' in e['error'] for e in events)
    assert client.runs == 1


def test_model_tool_event_saves_a_shared_note(setup_course, monkeypatch):
    from app.qa import notes
    client = FakeRuntime()
    original_request = client.request
    def request(method, params=None, **kwargs):
        result = original_request(method, params, **kwargs)
        if method == 'turn/start':
            events = []
            while not client.events.empty():
                events.append(client.events.get_nowait())
            events.insert(1, {'id': 99, 'method': 'item/tool/call', 'params': {
                'threadId': 'thread', 'turnId': result['turn']['id'], 'tool': 'write_note',
                'arguments': {'path': 'notes/review.md', 'text': 'Batch normalization. [T0001]', 'base_revision': None}}})
            for event in events:
                client.events.put(event)
        return result
    client.request = request
    monkeypatch.setattr(runtime, 'get_runtime', lambda: client)
    events = list(chat.ask(VID, '请保存一份复习笔记'))
    assert events[-1]['type'] == 'done'
    assert all(response['success'] for response in client.responses)
    assert any(event['type'] == 'note_changed' for event in events)
    assert notes.read(VID, 'review.md')['citations'] == [{'id': 'T0001', 'start': 0.0}]


def test_tool_start_and_commentary_arrive_before_completion(setup_course, monkeypatch):
    from app.qa.workspace import CourseWorkspace
    client = FakeRuntime()
    request, call = client.request, CourseWorkspace.call
    seen = []
    def checked_call(self, name, arguments):
        assert any(e['type'] == 'progress' and '核对课堂证据' in e['text'] for e in seen)
        return call(self, name, arguments)
    def streamed(method, params=None, **kwargs):
        result = request(method, params, **kwargs)
        if method == 'turn/start':
            queued = []
            while not client.events.empty():
                event = client.events.get_nowait()
                if (event.get('params', {}).get('item') or {}).get('phase') == 'commentary':
                    item = {'id': 'commentary', 'type': 'agentMessage', 'phase': 'commentary'}
                    queued.extend([
                        {'method': 'item/started', 'params': {'threadId': 'thread', 'item': item}},
                        {'method': 'item/agentMessage/delta', 'params': {
                            'threadId': 'thread', 'itemId': 'commentary', 'delta': '正在核对'}},
                    ])
                queued.append(event)
            for event in queued:
                client.events.put(event)
        return result
    client.request = streamed
    monkeypatch.setattr(runtime, 'get_runtime', lambda: client)
    monkeypatch.setattr(CourseWorkspace, 'call', checked_call)
    for event in chat.ask(VID, '核对公式'):
        seen.append(event)
    commentary = [e for e in seen if e['type'] == 'commentary']
    assert commentary[1] == {'type': 'commentary', 'text': '正在核对', 'append': True}
    assert seen[-1]['type'] == 'done'


@pytest.mark.parametrize('method,decision,expected', [
    ('item/commandExecution/requestApproval', 'accept', {'decision': 'accept'}),
    ('item/fileChange/requestApproval', 'decline', {'decision': 'decline'}),
    ('item/tool/requestUserInput', 'answer', {'answers': {'focus': {'answers': ['PDF为主']}}}),
    ('item/tool/requestUserInput', 'decline', {'answers': {}}),
])
def test_native_interaction_waits_for_user_and_rejects_stale_reply(setup_course, monkeypatch, method, decision, expected):
    client = FakeRuntime(answer='完成。', read=False)
    request = client.request
    def with_approval(name, params=None, **kwargs):
        result = request(name, params, **kwargs)
        if name == 'turn/start':
            queued = []
            while not client.events.empty():
                queued.append(client.events.get_nowait())
            client.events.put({'id': 88, 'method': method, 'params': {
                'threadId': 'thread', 'turnId': 'turn1', 'itemId': 'tool',
                'command': 'python check_formula.py', 'questions': [{'id': 'focus', 'question': '资料侧重什么？'}]}})
            for event in queued:
                client.events.put(event)
        return result
    client.request = with_approval
    monkeypatch.setattr(runtime, 'get_runtime', lambda: client)
    answer = chat.ask(VID, '检查公式')
    waiting = next(e['interaction'] for e in answer if e['type'] == 'interaction')
    assert client.responses == []
    with pytest.raises(ValueError):
        codex_chat.respond_interaction(VID, 'stale-id', decision, {})
    codex_chat.respond_interaction(VID, waiting['id'], decision, {'focus': 'PDF为主', 'unasked': 'ignored'})
    remaining = list(answer)
    assert client.responses == [expected] and remaining[-1]['type'] == 'done'
    with pytest.raises(ValueError):
        codex_chat.respond_interaction(VID, waiting['id'], decision, {})


def test_clear_removes_notes_after_root_migration_and_missing_evidence(setup_course, monkeypatch, tmp_path):
    client = FakeRuntime()
    monkeypatch.setattr(runtime, 'get_runtime', lambda: client)
    list(chat.ask(VID, 'Explain'))
    from app.qa.workspace import CourseWorkspace
    workspace = CourseWorkspace(VID, codex_chat.state(VID)['conversation_id'], settings.get())
    workspace.begin_turn('note')
    # 模拟升级前的对话笔记，而非新版 write_note 的视频级笔记。
    (workspace.root / 'notes/a.md').write_text('memo', encoding='utf-8')
    workspace.call('write_note', {'path': 'notes/shared.md', 'text': 'shared memo', 'base_revision': None})
    target = tmp_path / 'new'
    settings.update({'workspace_root': str(target)})
    copied = target / VID / workspace.conversation_id / 'notes' / 'a.md'
    assert copied.is_file()
    (artifacts.video_dir(VID) / 'evidence.json').unlink()
    chat.clear(VID)
    assert not copied.exists() and not (workspace.root / 'notes').exists()
    assert (artifacts.video_dir(VID) / 'notes/shared.md').read_text() == 'shared memo'
    assert list((artifacts.video_dir(VID) / 'notes').glob('旧对话-*.md'))
    assert chat.history(VID) == []


def test_sse_generator_can_finish_on_another_worker_thread(setup_course, monkeypatch):
    monkeypatch.setattr(runtime, 'get_runtime', lambda: FakeRuntime())
    answer = chat.ask(VID, 'Explain')
    with ThreadPoolExecutor(1) as first, ThreadPoolExecutor(1) as second:
        assert first.submit(next, answer).result()['type'] == 'progress'
        assert second.submit(list, answer).result()[-1]['type'] == 'done'
    assert not codex_chat.busy()


def test_state_write_failure_still_releases_conversation(setup_course, monkeypatch):
    monkeypatch.setattr(runtime, 'get_runtime', lambda: FakeRuntime(account='apiKey'))
    def full_disk(*_):
        raise OSError('disk full')
    monkeypatch.setattr(db, 'set_setting', full_disk)
    with pytest.raises(OSError):
        list(chat.ask(VID, 'Explain'))
    assert not codex_chat.busy()
    lock = codex_chat.conversation_lock(VID)
    assert lock.acquire(blocking=False)
    lock.release()
