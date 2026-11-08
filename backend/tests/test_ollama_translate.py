import httpx
import pytest
from fastapi.testclient import TestClient

from app import artifacts, config, db
from app.pipeline import translate, send_plan
from app.pipeline.base import StageContext, StageError


def fake_http(monkeypatch, *, outputs=None, models=None):
    from app.pipeline import ollama_translate as local
    calls = []
    results = iter(outputs or ['译文'])
    def request(method, url, **kwargs):
        assert url.startswith('http://127.0.0.1:11434/api/')
        assert kwargs['trust_env'] is False and kwargs['follow_redirects'] is False
        assert 'headers' not in kwargs
        calls.append((method, url, kwargs))
        if url.endswith('/tags'):
            body = {'models': models if models is not None else [
                {'name': config.settings.ollama_translate.model, 'size': 4624649686}]}
        else:
            out = next(results)
            if isinstance(out, Exception):
                raise out
            body = out if isinstance(out, dict) else {'response': out, 'done': True, 'done_reason': 'stop'}
        return httpx.Response(200, json=body)
    monkeypatch.setattr(local.httpx, 'request', request)
    return calls


def test_local_checkpoint_and_resume(data_dir, monkeypatch):
    db.set_setting('translation_provider', 'ollama')
    calls = fake_http(monkeypatch, outputs=['保留 attention。', httpx.ReadTimeout('private')])
    cues = [{'id': str(i), 'text': text, 'zh': zh, 'start': i, 'end': i+1}
            for i, (text, zh) in enumerate([('old', '已有'), ('attention matters', None), ('next', None)])]
    artifacts.write_artifact('local', 'cues.json', cues, {})
    with pytest.raises(StageError, match='超时'):
        translate.run(StageContext('local', 'translate'))
    saved = artifacts.read_artifact('local', 'cues.json')
    assert [c['zh'] for c in saved] == ['已有', '保留 attention。', None]
    payload = calls[1][2]['json']
    assert payload['model'] == config.settings.ollama_translate.model
    assert payload['stream'] is False and '只输出' in payload['prompt']
    assert 'attention matters' in payload['prompt']
    calls = fake_http(monkeypatch, outputs=['下一条'])
    translate.run(StageContext('local', 'translate'))
    assert len(calls) == 2  # 模型检查 + 只补一条
    assert (config.settings.videos_dir / 'local/subs.bilingual.vtt').exists()
    assert config.settings.stage_params('translate')['batch_size'] == 1
    assert not db.fetch_all('SELECT * FROM api_usage WHERE cost_usd > 0')


@pytest.mark.parametrize('body', [{}, {'response': '', 'done': True},
    {'response': '半句', 'done': True, 'done_reason': 'length'}, {'response': '半句', 'done': False}])
def test_bad_output_not_saved(data_dir, monkeypatch, body):
    from app.pipeline.ollama_translate import OllamaTranslator
    fake_http(monkeypatch, outputs=[body])
    with pytest.raises(StageError):
        OllamaTranslator().translate([{'id': '1', 'text': 'hello'}], [], [])


@pytest.mark.parametrize('models', [[], [{'name': 'hf.co/tencent/Hy-MT2-7B-GGUF:Q4_K_M', 'remote_host': 'https://ollama.com'}]])
def test_missing_or_cloud_model_blocks(data_dir, monkeypatch, models):
    from app.pipeline.ollama_translate import OllamaTranslator
    calls = fake_http(monkeypatch, models=models)
    with pytest.raises(StageError):
        OllamaTranslator()
    assert len(calls) == 1


def test_settings_and_plan(data_dir, monkeypatch):
    from app.main import app
    calls = fake_http(monkeypatch)
    client = TestClient(app)
    result = client.put('/api/settings', json={'translator': 'ollama'})
    assert result.status_code == 200
    assert result.json()['ollama']['model'] == config.settings.ollama_translate.model
    assert client.post('/api/settings/test', json={'service': 'ollama'}).json()['ok']
    plan = {'blocking': [], 'warnings': []}
    item = send_plan._translate_item([{'text': 'hello', 'zh': None}], plan)
    assert item['local'] and item['estimated_cost_usd'] == 0 and not plan['blocking']
    assert all(url.endswith('/tags') for _, url, _ in calls)


@pytest.mark.parametrize('response', [httpx.ConnectError('secret'), httpx.Response(302),
    httpx.Response(200, text='not json'), httpx.Response(200, json=[]),
    httpx.Response(200, json={'error': 'secret'})])
def test_protocol_errors_are_safe(data_dir, monkeypatch, response):
    from app.pipeline import ollama_translate as local
    def request(*args, **kwargs):
        if isinstance(response, Exception):
            raise response
        return response
    monkeypatch.setattr(local.httpx, 'request', request)
    with pytest.raises(StageError) as error:
        local.check_model()
    assert 'secret' not in str(error.value)


def test_long_input_does_not_generate(data_dir, monkeypatch):
    from app.pipeline.ollama_translate import OllamaTranslator
    calls = fake_http(monkeypatch)
    with pytest.raises(StageError, match='太长'):
        OllamaTranslator().translate([{'id': '1', 'text': 'x' * 2001}], [], [])
    assert len(calls) == 1


def test_runner_failure_retries_same_cue_and_saves_once(data_dir, monkeypatch):
    from app.pipeline import ollama_translate as local
    db.set_setting('translation_provider', 'ollama')
    artifacts.write_artifact('retry', 'cues.json', [
        {'id': '1', 'text': 'old', 'zh': '已有', 'start': 0, 'end': 1},
        {'id': '2', 'text': 'hello', 'zh': None, 'start': 1, 'end': 2}], {})
    payloads, sleeps = [], []
    failures = [
        'Post "http://127.0.0.1:8262/tokenize": dial tcp 127.0.0.1:8262: connectex: failed',
        'model runner has unexpectedly stopped, check ollama server logs for details',
        'health resp: Get "http://127.0.0.1:8262/health": dial tcp 127.0.0.1:8262: connectex: failed',
        'Post "http://127.0.0.1:8262/completion": dial tcp 127.0.0.1:8262: connectex: failed',
    ]
    def request(method, url, **kwargs):
        if url.endswith('/tags'):
            return httpx.Response(200, json={'models': [
                {'name': config.settings.ollama_translate.model, 'size': 1}]})
        payloads.append(kwargs['json'])
        if len(payloads) <= len(failures):
            return httpx.Response(500, json={'error': failures[len(payloads) - 1]})
        return httpx.Response(200, json={'response': '你好', 'done': True, 'done_reason': 'stop'})
    monkeypatch.setattr(local.httpx, 'request', request)
    import time
    monkeypatch.setattr(time, 'sleep', sleeps.append)
    translate.run(StageContext('retry', 'translate'))
    assert len(payloads) == 5 and all(p == payloads[0] for p in payloads)
    assert len(sleeps) == 4 and all(delay > 0 for delay in sleeps)
    assert [c['zh'] for c in artifacts.read_artifact('retry', 'cues.json')] == ['已有', '你好']
    assert len(db.fetch_all("SELECT * FROM api_usage WHERE video_id = 'retry'")) == 1


@pytest.mark.parametrize('body,status,retries', [
    ({'error': 'dial tcp 127.0.0.1:8262: connectex: private'}, 500, True),
    ({'error': 'out of memory private'}, 500, False),
    ({'error': 'model not found private'}, 404, False),
    ({'error': 'dial tcp example.com:80: private'}, 500, False),
    (['private'], 500, False),
])
def test_runner_retries_are_bounded_and_specific(data_dir, monkeypatch, body, status, retries):
    from app.pipeline import ollama_translate as local
    calls = []
    def request(*args, **kwargs):
        calls.append(kwargs)
        return httpx.Response(status, json=body)
    monkeypatch.setattr(local.httpx, 'request', request)
    import time
    monkeypatch.setattr(time, 'sleep', lambda _: None)
    with pytest.raises(StageError) as error:
        local.request('/api/generate', {'prompt': 'hello'})
    assert len(calls) == (5 if retries else 1)
    assert 'private' not in str(error.value)


@pytest.mark.parametrize('error_type', [httpx.ConnectError, httpx.ConnectTimeout])
def test_local_connection_recovers_within_same_request(data_dir, monkeypatch, error_type):
    from app.pipeline import ollama_translate as local
    calls = []
    def request(*args, **kwargs):
        calls.append(kwargs['json'])
        if len(calls) == 1:
            raise error_type('private')
        return httpx.Response(200, json={'response': '你好', 'done': True, 'done_reason': 'stop'})
    monkeypatch.setattr(local.httpx, 'request', request)
    import time
    monkeypatch.setattr(time, 'sleep', lambda _: None)
    assert local.request('/api/generate', {'prompt': 'hello'})['response'] == '你好'
    assert calls == [{'prompt': 'hello'}] * 2
