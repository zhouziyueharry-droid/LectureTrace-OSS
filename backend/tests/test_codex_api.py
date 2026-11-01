"""Account and SSE boundaries, with no live login or model request."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import codex, chat as chat_api
from app.qa import codex_chat


@pytest.fixture
def client(tmp_settings):
    app = FastAPI()
    app.include_router(codex.router)
    return TestClient(app)


def fake_runtime(monkeypatch, account=None, auth_url='https://auth.openai.com/login'):
    calls = []

    def request(method, params=None, **kwargs):
        calls.append((method, params))
        if method == 'account/read':
            return {'account': account}
        if method == 'account/login/start':
            return {'loginId': 'login', 'authUrl': auth_url}
        return {'rateLimits': None}

    instance = SimpleNamespace(version='0.157.1', _login_result=None, request=request)
    monkeypatch.setattr(codex.runtime, 'get_runtime', lambda: instance)
    return calls


def test_status_redacts_account_secrets_and_keeps_unknown_quota(client, monkeypatch):
    fake_runtime(monkeypatch, {'type': 'chatgpt', 'email': 'student@example.com', 'planType': 'plus',
                              'accessToken': 'never-return-this'})
    result = client.get('/api/codex/status').json()
    assert result['account'] == {'type': 'chatgpt', 'email': 'student@example.com', 'planType': 'plus'}
    assert result['rate_limits'] == {'rateLimits': None}
    assert result['isolation_verified'] is False


def test_unofficial_login_url_is_cancelled(client, monkeypatch):
    calls = fake_runtime(monkeypatch, auth_url='https://example.com/login')
    assert client.post('/api/codex/login').status_code == 502
    assert ('account/login/cancel', {'loginId': 'login'}) in calls


def test_api_account_cannot_list_chatgpt_models(client, monkeypatch):
    calls = fake_runtime(monkeypatch, {'type': 'apiKey'})
    assert client.get('/api/codex/models').json()['models'] == []
    assert not any(method == 'model/list' for method, _ in calls)


def test_settings_ignore_retired_budget_and_reject_active_change(client, monkeypatch):
    result = client.put('/api/codex/settings', json={'max_images': -1})
    assert result.status_code == 200 and 'max_images' not in result.json()
    assert client.put('/api/codex/settings', json={'max_tool_calls': True}).status_code == 422
    monkeypatch.setattr(codex_chat, 'busy', lambda: True)
    assert client.put('/api/codex/settings', json={'provider': 'codex'}).status_code == 409


@pytest.mark.parametrize('event', [{'type': 'error', 'error': 'busy'}, {'type': 'done', 'message': {}}])
def test_terminal_sse_request_does_not_interrupt_other_turn(monkeypatch, event):
    stops = []
    monkeypatch.setattr(chat_api, 'get_video', lambda _: {})
    monkeypatch.setattr(chat_api, '_qa', lambda: SimpleNamespace(ask=lambda *args: iter([event])))
    monkeypatch.setattr(codex_chat, 'stop', lambda vid: stops.append(vid))

    async def consume():
        response = await chat_api.chat_ask('video', chat_api.AskBody(question='hello'))
        return [item async for item in response.body_iterator]

    assert asyncio.run(consume())
    assert stops == []
