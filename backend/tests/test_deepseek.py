"""DeepSeek 使用独立凭据；只接入幻灯片与大纲，不发真实网络请求。"""
import dataclasses
import json

import httpx
import openai
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app import config, db, keys
from app.llm import client, openai_compat as oc, settings, tokens
from app.pipeline import send_plan


@pytest.fixture(autouse=True)
def isolated_deepseek_key(tmp_settings, set_settings):
    set_settings(dataclasses.replace(config.settings, deepseek_api_key=''))


@pytest.mark.parametrize('status,models,ok', [(200, ['deepseek-flash'], True), (200, ['other'], False), (401, [], False)])
def test_connection_only_lists_models(tmp_settings, monkeypatch, status, models, ok):
    from app.main import app
    keys.update({'deepseek': 'test-key'})
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(status, json={'object': 'list', 'data': [
            {'id': model, 'object': 'model', 'created': 0, 'owned_by': 'deepseek'} for model in models]})
    real_openai = openai.OpenAI
    monkeypatch.setattr(openai, 'OpenAI', lambda **kw: real_openai(
        **kw, http_client=httpx.Client(transport=httpx.MockTransport(handle))))
    result = TestClient(app).post('/api/settings/test', json={'service': 'deepseek'}).json()
    assert result['ok'] is ok
    assert len(calls) == 1 and calls[0].method == 'GET'
    assert str(calls[0].url) == 'https://api.deepseek.com/models'


def test_settings_keep_other_services(tmp_settings):
    from app.main import app
    api = TestClient(app)
    keys.update({'openai': 'openai-original'})
    before = settings.model_for('qa')
    result = api.put('/api/settings', json={'deepseek': 'deepseek-secret-1234'}).json()
    assert result['keys']['deepseek']['key_hint'] == '…1234'
    assert 'deepseek-secret' not in json.dumps(result)
    assert keys.get_key('openai') == 'openai-original'
    assert api.put('/api/settings/llm', json={'models': {
        'read_slides': 'deepseek-flash', 'outline': 'deepseek-flash'}}).status_code == 200
    assert settings.model_for('qa') == before
    assert api.put('/api/settings/llm', json={'models': {'qa': 'deepseek-flash'}}).status_code == 400
    assert settings.model_for('qa') == before
    assert config.settings.stage_params('read_slides')['provider'] == 'deepseek'
    assert api.put('/api/settings', json={'deepseek': ''}).json()['keys']['deepseek']['has_key'] is False


@pytest.mark.parametrize('effort,expected', [('none', None), ('low', 'low'), ('medium', 'high'), ('high', 'high'), ('xhigh', 'high'), ('max', 'max')])
def test_request_parameters(tmp_settings, effort, expected):
    req = oc.build_request(model='deepseek-flash', messages=[{'role': 'user', 'content': 'JSON'}],
                           system=None, max_tokens=8000, effort=effort, json_mode=True,
                           cache_key='do-not-send', service_tier='flex')
    assert req['extra_body'] == {'thinking': {'type': 'disabled' if effort == 'none' else 'enabled'}}
    assert req.get('reasoning_effort') == expected
    assert req['max_tokens'] == 8000 and 'max_completion_tokens' not in req
    assert 'service_tier' not in req
    assert req['response_format'] == {'type': 'json_object'}


def test_sdk_routing_and_images(tmp_settings, set_settings, monkeypatch):
    set_settings(dataclasses.replace(config.settings, llm_provider='openai'))
    keys.update({'openai': 'openai-original', 'deepseek': 'deepseek-test'})
    settings.update({'models': {'read_slides': 'deepseek-flash', 'outline': 'deepseek-flash'}})
    calls = []

    def handle(request):
        body = json.loads(request.content)
        calls.append((request, body))
        return httpx.Response(200, json={'id': 'test', 'object': 'chat.completion', 'created': 0,
            'model': body['model'], 'choices': [{'index': 0, 'finish_reason': 'stop',
                'message': {'role': 'assistant', 'content': '{"ok":true}', 'reasoning_content': 'not the answer'}}],
            'usage': {'prompt_tokens': 1000, 'completion_tokens': 100, 'prompt_cache_hit_tokens': 400}})

    real_openai = openai.OpenAI
    monkeypatch.setattr(openai, 'OpenAI', lambda **kw: real_openai(
        **kw, http_client=httpx.Client(transport=httpx.MockTransport(handle))))
    monkeypatch.setattr(oc, '_client', None)
    class Result(BaseModel):
        ok: bool

    image = {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/jpeg', 'data': 'QUJD'}}
    for stage in ('read_slides', 'outline'):
        assert client.complete_structured(messages=[{'role': 'user', 'content': [image]}],
            response_model=Result, video_id=None, stage=stage, effort='low').ok
    for stage in ('qa', 'translate'):
        client.complete(messages=[{'role': 'user', 'content': 'hello'}], video_id=None, stage=stage)
    for request, body in calls[:2]:
        assert request.url.host == 'api.deepseek.com'
        assert request.headers['authorization'] == 'Bearer deepseek-test'
        assert body['response_format'] == {'type': 'json_object'}
        assert body['messages'][-1]['content'][0]['image_url']['url'] == 'data:image/jpeg;base64,QUJD'
    for request, body in calls[2:]:
        assert request.url.host == 'api.openai.com'
        assert request.headers['authorization'] == 'Bearer openai-original'
        assert body['model'] == settings.model_for('qa')
    rows = db.fetch_all('SELECT * FROM api_usage')
    assert rows[0]['provider'] == 'DeepSeek' and rows[0]['cache_read_tokens'] == 400
    assert rows[0]['cost_usd'] == pytest.approx((600 * .3 + 400 * .006 + 100 * 1.2) / 1e6)


def test_plan_and_missing_key(tmp_settings, monkeypatch):
    settings.update({'models': {'read_slides': 'deepseek-flash', 'outline': 'deepseek-flash'}})
    monkeypatch.setattr(send_plan, '_read', lambda video, name: [{'id': 'C0001', 'text': 'hi', 'zh': '嗨'}] if name == 'cues.json' else [{'id': 'S001'}])
    estimate = lambda *args: {'model': 'deepseek-flash', 'effort': 'low', 'requests': [(100, 1024)], 'output_range': (10, 20), 'desc': 'test'}
    monkeypatch.setattr(send_plan, '_read_slides_estimate', estimate)
    monkeypatch.setattr(send_plan, '_outline_estimate', estimate)
    plan = send_plan.build_send_plan('v')
    assert any('DeepSeek' in message for message in plan['blocking'])
    assert not any('OpenAI API Key' in message for message in plan['blocking'])
    keys.update({'deepseek': 'test'})
    plan = send_plan.build_send_plan('v')
    assert not plan['blocking']
    assert all(i['provider'].startswith('DeepSeek') and i['service_tier'] == 'standard' for i in plan['items'][1:])
    assert tokens.image_tokens(1920, 1080, 'deepseek-flash') == 1024
    with pytest.raises(client.LLMError, match='DeepSeek'):
        keys.update({'deepseek': ''})
        oc.get_client('deepseek')
