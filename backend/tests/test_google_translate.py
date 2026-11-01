import io
import json
import dataclasses
from urllib.error import HTTPError, URLError

import pytest
from fastapi.testclient import TestClient

from app import artifacts, config, db, keys
from app.pipeline import translate, send_plan
from app.pipeline.base import StageContext, StageError


@pytest.fixture
def client(data_dir):
    from app.main import app
    return TestClient(app, raise_server_exceptions=True)


def google_mode(set_settings):
    set_settings(dataclasses.replace(config.settings, translate=config.TranslateParams(provider='google')))
    keys.update({'google': 'secret-google-key'})


def test_google_request_and_checkpoint(data_dir, set_settings, monkeypatch):
    google_mode(set_settings)
    calls = []
    def request(req, timeout):
        calls.append(req)
        assert timeout == 60
        assert req.get_header('X-goog-api-key') == 'secret-google-key'
        assert 'secret-google-key' not in req.full_url
        payload = json.loads(req.data)
        assert payload == {'q': ['A < B & C'], 'target': 'zh-CN', 'format': 'text', 'model': 'nmt'}
        return io.BytesIO(json.dumps({'data': {'translations': [{'translatedText': '甲 &lt; 乙 &amp; 丙'}]}}).encode())
    monkeypatch.setattr(translate, 'urlopen', request)
    cues = [{'id': 'C1', 'start': 0, 'end': 1, 'text': 'old', 'zh': '保留'},
            {'id': 'C2', 'start': 1, 'end': 2, 'text': 'A < B & C', 'zh': None}]
    artifacts.write_artifact('google-test', 'cues.json', cues, {})
    ctx = StageContext('google-test', 'translate')
    translate.run(ctx)
    assert ctx.read('cues.json')[0]['zh'] == '保留'
    assert ctx.read('cues.json')[1]['zh'] == '甲 < 乙 & 丙'
    assert len(calls) == 1
    assert ctx.params['provider'] == 'google' and ctx.params['model'] is None
    assert db.fetch_one('SELECT provider, cost_usd FROM api_usage')['provider'] == 'Google Translate'


@pytest.mark.parametrize('body', [{}, {'data': {'translations': []}}, {'data': {'translations': [{'translatedText': ''}]}}, {'data': {'translations': [None]}}])
def test_invalid_response_does_not_save_translation(data_dir, monkeypatch, body):
    monkeypatch.setattr(translate, 'urlopen', lambda *a, **kw: io.BytesIO(json.dumps(body).encode()))
    with pytest.raises(StageError, match='返回'):
        translate.GoogleTranslator('secret').translate([{'id': 'C1', 'text': 'hello'}], [], [])


@pytest.mark.parametrize('error', [HTTPError('https://example.com?key=secret', 403, 'secret', {}, None), URLError('secret')])
def test_errors_never_expose_key(data_dir, monkeypatch, error):
    def fail(*a, **kw):
        raise error
    monkeypatch.setattr(translate, 'urlopen', fail)
    with pytest.raises(StageError) as caught:
        translate.GoogleTranslator('secret').translate([{'id': 'C1', 'text': 'hello'}], [], [])
    assert 'secret' not in str(caught.value)


def test_request_splits_at_google_limit(data_dir, monkeypatch):
    sizes = []
    def request(req, **kw):
        q = json.loads(req.data)['q']
        sizes.append(len(q))
        assert len(req.data) <= 100000
        return io.BytesIO(json.dumps({'data': {'translations': [{'translatedText': '译'} for _ in q]}}).encode())
    monkeypatch.setattr(translate, 'urlopen', request)
    batch = [{'id': str(i), 'text': 'a'} for i in range(129)]
    assert len(translate.GoogleTranslator('key').translate(batch, [], [])) == 129
    assert sizes == [128, 1]


def test_google_settings_and_plan(client, set_settings):
    response = client.put('/api/settings', json={'google': 'secret-abcd', 'translator': 'google'})
    assert response.status_code == 200
    assert response.json()['translator'] == 'google'
    assert response.json()['keys']['google'] == {'has_key': True, 'key_hint': '…abcd', 'source': 'settings'}
    assert 'secret-abcd' not in response.text
    assert config.settings.stage_params('translate')['provider'] == 'google'
    plan = {'blocking': [], 'warnings': []}
    item = send_plan._translate_item([{'text': 'a' * 100, 'zh': None}], plan)
    assert item['provider'] == 'Google Cloud Translation'
    assert item['estimated_cost_usd'] == 0.002
    assert plan['warnings'] and not plan['blocking']
    assert client.put('/api/settings', json={'translator': 'invalid', 'google': 'replacement'}).status_code == 422
    assert keys.get_key('google') == 'secret-abcd'
    client.put('/api/settings', json={'google': ''})
    plan = {'blocking': [], 'warnings': []}
    send_plan._translate_item([{'text': 'a', 'zh': None}], plan)
    assert 'Google' in plan['blocking'][0]
    plan = {'blocking': [], 'warnings': []}
    assert send_plan._translate_item([{'text': 'a', 'zh': '已译'}], plan)['skipped']
    assert not plan['blocking']


def test_connection_test_only_lists_languages(client, monkeypatch):
    calls = []
    def request(req, **kw):
        calls.append(req)
        assert req.get_method() == 'GET' and req.data is None
        assert req.full_url.endswith('/languages')
        return io.BytesIO(b'{"data":{"languages":[{"language":"zh-CN"}]}}')
    monkeypatch.setattr(translate, 'urlopen', request)
    client.put('/api/settings', json={'google': 'test-key'})
    assert client.post('/api/settings/test', json={'service': 'google'}).json()['ok']
    assert len(calls) == 1
    assert not db.fetch_all('SELECT * FROM api_usage')


def test_switch_provider_invalidates_old_consent_and_blocks_during_processing(client):
    db.execute("INSERT INTO videos (id,title,filename,created_at,status) VALUES ('v','t','t.mp4',?,'failed')", (db.now_iso(),))
    db.execute("INSERT INTO stages (video_id,name,status) VALUES ('v','translate','failed')")
    db.execute("INSERT INTO stages (video_id,name,status) VALUES ('v','await_confirm','done')")
    assert client.put('/api/settings', json={'translator': 'google'}).status_code == 200
    assert db.fetch_one("SELECT status FROM stages WHERE name='await_confirm'")['status'] == 'pending'
    db.execute("UPDATE videos SET status='processing' WHERE id='v'")
    assert client.put('/api/settings', json={'translator': 'deepl'}).status_code == 409
    assert config.settings.translation_provider() == 'google'


def test_google_bytes_limit_and_failure_checkpoint(data_dir, set_settings, monkeypatch):
    google_mode(set_settings)
    calls = []
    def request(req, **kw):
        calls.append(req)
        assert len(req.data) <= 100000
        if len(calls) == 2:
            raise URLError('offline')
        q = json.loads(req.data)['q']
        return io.BytesIO(json.dumps({'data': {'translations': [{'translatedText': '译'} for _ in q]}}).encode())
    monkeypatch.setattr(translate, 'urlopen', request)
    batch = [{'id': str(i), 'text': '字' * 20000} for i in range(2)]
    with pytest.raises(StageError):
        translate.GoogleTranslator('key').translate(batch, [], [])
    assert len(calls) == 2
    calls.clear()
    cues = [{'id': str(i), 'text': 'hello', 'zh': None, 'start': i, 'end': i + 1} for i in range(41)]
    artifacts.write_artifact('v', 'cues.json', cues, {})
    with pytest.raises(StageError):
        translate.run(StageContext('v', 'translate'))
    assert sum(bool(c['zh']) for c in artifacts.read_artifact('v', 'cues.json')) == 40
