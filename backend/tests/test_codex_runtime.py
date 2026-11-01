"""运行时边界：不启动真实 Codex，不调用模型。"""
import json
import queue
import threading
from io import StringIO

import pytest

from app.codex.runtime import CodexError, Runtime, child_environment
from app.codex import runtime as module, settings


def bare_runtime():
    runtime = Runtime.__new__(Runtime)
    runtime._lock = threading.RLock()
    runtime._write_lock = threading.Lock()
    runtime._pending = {}
    runtime._subscriptions = {}
    runtime._parents = {}
    runtime._closed = False
    runtime._login_result = None
    runtime.process = type('Process', (), {'stdin': StringIO()})()
    return runtime


def test_secret_environment_is_not_inherited(monkeypatch, tmp_path):
    monkeypatch.setenv('OPENAI_API_KEY', 'never-inherit')
    monkeypatch.setenv('CODEX_API_KEY', 'never-inherit')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'never-inherit')
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'never-inherit')
    env = child_environment(tmp_path)
    assert env['CODEX_HOME'] == str(tmp_path)
    assert not any(k in env for k in ('OPENAI_API_KEY', 'CODEX_API_KEY', 'ANTHROPIC_API_KEY', 'AWS_SECRET_ACCESS_KEY'))


def test_responses_and_events_are_routed_independently():
    runtime = bare_runtime()
    response = queue.Queue()
    runtime._pending[1] = response
    first = runtime.subscribe('thread-a')
    second = runtime.subscribe('thread-b')
    runtime._dispatch({'method': 'turn/completed', 'params': {'threadId': 'thread-b'}})
    runtime._dispatch({'id': 1, 'result': {'ok': True}})
    assert response.get_nowait()['result'] == {'ok': True}
    assert first.empty()
    assert second.get_nowait()['params']['threadId'] == 'thread-b'


def test_approval_and_unowned_tool_requests_are_denied():
    runtime = bare_runtime()
    runtime._dispatch({'id': 2, 'method': 'item/commandExecution/requestApproval', 'params': {}})
    runtime._dispatch({'id': 3, 'method': 'item/tool/call', 'params': {'threadId': 'unknown'}})
    responses = [json.loads(line) for line in runtime.process.stdin.getvalue().splitlines()]
    assert responses[0]['result'] == {'decision': 'decline'}
    assert responses[1]['error']['code'] == -32601


def test_process_exit_wakes_requests_and_active_turns():
    runtime = bare_runtime()
    response = queue.Queue()
    runtime._pending[1] = response
    events = runtime.subscribe('thread-a')
    runtime._fail('Codex 进程已退出')
    assert response.get_nowait()['error']['message'] == 'Codex 进程已退出'
    assert events.get_nowait()['method'] == 'runtime/closed'
    with pytest.raises(CodexError):
        runtime.subscribe('thread-b')


def test_child_tools_and_approvals_route_to_their_parent_only():
    runtime = bare_runtime()
    first, second = runtime.subscribe('parent'), runtime.subscribe('unrelated')
    runtime._dispatch({'method': 'thread/started', 'params': {'thread': {
        'id': 'child', 'source': {'subAgent': {'thread_spawn': {'parent_thread_id': 'parent'}}}}}})
    for method in ('item/tool/call', 'item/commandExecution/requestApproval', 'item/tool/requestUserInput'):
        message = {'id': method, 'method': method, 'params': {'threadId': 'child'}}
        runtime._dispatch(message)
        assert first.get_nowait() == message
        assert second.empty()
    assert runtime.process.stdin.getvalue() == ''


def test_rebuilding_runtime_closes_previous_process(monkeypatch):
    old = bare_runtime()
    old._closed = True
    closed = []
    old.close = lambda: closed.append(True)
    replacement = object()
    monkeypatch.setattr(module, '_runtime', old)
    monkeypatch.setattr(module, 'Runtime', lambda: replacement)
    assert module.get_runtime() is replacement
    assert closed == [True]


def test_native_environment_preserves_write_sandbox_and_approvals():
    good = {'thread': {'environments': [{'environmentId': 'local'}]}, 'approvalPolicy': 'on-request', 'approvalsReviewer': 'user',
            'sandbox': {'type': 'workspaceWrite', 'networkAccess': True},
            'modelProvider': 'openai', 'instructionSources': []}
    module.verify_thread(good)
    for changed in ({'thread': {'environments': None}}, {'thread': {}},
                    {'thread': {'environments': []}}, {'approvalPolicy': 'never'},
                    {'sandbox': {'type': 'dangerFullAccess'}}, {'modelProvider': 'anthropic'},
                    {'sandbox': {'type': 'readOnly', 'networkAccess': True}},
                    {'approvalsReviewer': 'auto_review'}):
        with pytest.raises(CodexError):
            module.verify_thread({**good, **changed})


def test_workspace_setting_rejects_non_string(tmp_settings):
    with pytest.raises(ValueError):
        settings.update({'workspace_root': []})


@pytest.mark.parametrize('failure', [None, 'config', 'host', 'missing'])
def test_native_configuration_keeps_skills_and_default_capabilities(monkeypatch, failure):
    runtime = bare_runtime()
    state = {'skill_enabled': True, 'writes': []}
    def request(method, params):
        if method == 'config/read':
            return {'config': {'model_provider': 'openai', 'forced_login_method': 'chatgpt',
                              'approval_policy': 'on-request', 'approvals_reviewer': 'user',
                              'sandbox_mode': 'workspace-write',
                              'web_search': 'disabled' if failure == 'config' else 'live'}}
        if method == 'experimentalFeature/list':
            if failure == 'missing':
                return {'data': []}
            return {'data': [{'name': k, 'enabled': True} for k in ('shell_tool', 'multi_agent', 'new_native_tool')]
                    + [{'name': 'skip_host_skill_discovery', 'enabled': False},
                       {'name': 'code_mode_host', 'enabled': failure != 'host'}], 'nextCursor': None}
        if method == 'mcpServerStatus/list':
            return {'data': [{'name': 'external'}] if failure == 'mcp' else [], 'nextCursor': None}
        if method == 'skills/list':
            return {'data': [{'skills': [{'enabled': state['skill_enabled'],
                                         'path': 'C:/host/skill/SKILL.md'}], 'errors': []}]}
        if method == 'skills/config/write':
            state['writes'].append(params)
            state['skill_enabled'] = False
            return {'effectiveEnabled': failure == 'skills'}
        raise AssertionError(method)
    monkeypatch.setattr(runtime, 'request', request)
    if failure:
        with pytest.raises(CodexError):
            runtime.verify_config()
    else:
        runtime.verify_config()
        assert state['writes'] == [] and state['skill_enabled']


def test_settings_changes_invalidate_threads_without_clearing_conversations(tmp_settings):
    from app import db
    stored = {'conversation_id': 'conversation', 'thread_id': 'old-thread', 'fingerprint': 'old-settings',
              'evidence_version': 'v1', 'outline_revision': 'r1', 'token_total': {'total': 20}}
    db.set_setting('codex_conversation:123456789abc', stored)
    settings.update({'provider': 'codex'})
    actual = db.get_setting('codex_conversation:123456789abc')
    assert actual['conversation_id'] == 'conversation'
    assert not actual.get('thread_id') and not actual.get('fingerprint')
    assert actual['token_total'] == {'total': 20}
    db.set_setting('codex_conversation:123456789abc', stored)
    settings.update({'provider': 'codex'})
    assert db.get_setting('codex_conversation:123456789abc') == stored


def test_workspace_changes_copy_notes_and_preserve_original(tmp_settings, tmp_path):
    from app import db
    previous, target = tmp_path / 'original', tmp_path / 'new'
    settings.update({'workspace_root': str(previous)})
    db.set_setting('codex_conversation:123456789abc', {'conversation_id': 'conversation', 'thread_id': 'old'})
    source = previous / '123456789abc' / 'conversation' / 'notes' / 'note.md'
    source.parent.mkdir(parents=True)
    source.write_text('Keep this note', encoding='utf8')
    settings.update({'workspace_root': str(target)})
    assert (target / source.relative_to(previous)).read_text(encoding='utf8') == 'Keep this note'
    assert source.read_text(encoding='utf8') == 'Keep this note'
    state = db.get_setting('codex_conversation:123456789abc')
    assert state['notes_roots'] == [str(previous.resolve()), str(target.resolve())]


def test_settings_validate_workspace_boundary_and_budget(tmp_settings):
    from app.config import ROOT_DIR
    assert settings.get()['provider'] == 'api'
    with pytest.raises(ValueError):
        settings.update({'workspace_root': str(ROOT_DIR / 'workspace')})
    assert 'max_read_tokens' not in settings.update({'max_read_tokens': 0})
    with pytest.raises(ValueError):
        settings.update({'provider': 'unknown'})


@pytest.mark.parametrize('version', ['0.157.1', '0.160.0', '0.999.0'])
def test_version_is_diagnostic_not_a_gate(monkeypatch, version):
    from types import SimpleNamespace
    monkeypatch.setattr(module, 'executable', lambda: 'codex')
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **kw:
                        SimpleNamespace(stdout='codex-cli ' + version, returncode=0))
    assert module.installed_version() == version


@pytest.mark.parametrize('stdout,code', [('', 0), ('codex-cli 0.160.0', 1)])
def test_invalid_version_command_fails(monkeypatch, stdout, code):
    from types import SimpleNamespace
    monkeypatch.setattr(module, 'executable', lambda: 'codex')
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **kw:
                        SimpleNamespace(stdout=stdout, returncode=code))
    with pytest.raises(CodexError, match='有效版本'):
        module.installed_version()
