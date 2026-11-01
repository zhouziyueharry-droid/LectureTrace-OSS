"""独立 Codex 账户与问答设置；不返回或读取任何令牌。"""
from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from ..codex import runtime, settings

router = APIRouter(prefix='/api/codex', tags=['codex'])


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    provider: str | None = None
    model: str | None = None
    effort: str | None = None
    workspace_root: str | None = None
    max_tool_calls: int | None = None
    max_read_tokens: int | None = None
    max_images: int | None = None


class CancelLogin(BaseModel):
    login_id: str


@router.get('/settings')
def get_settings():
    return settings.get()


@router.put('/settings')
def put_settings(body: SettingsUpdate):
    from ..qa import codex_chat, chat_runs
    if codex_chat.busy() or chat_runs.is_running():
        raise HTTPException(409, '请先停止当前问答，再更改运行时设置')
    try:
        return settings.update(body.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


def _request(method, params=None):
    try:
        return runtime.get_runtime().request(method, params)
    except runtime.CodexError as e:
        raise HTTPException(503, str(e)) from e
    except OSError as e:
        raise HTTPException(503, '无法访问独立 Codex 配置目录') from e


def public_account(account):
    if not account:
        return None
    return {k: account.get(k) for k in ('type', 'email', 'planType')}


@router.get('/status')
def status():
    result = {'available': False, 'version': None, 'account': None, 'rate_limits': None,
              'workspace_mode': 'native', 'isolation_verified': False, 'error': None}
    try:
        client = runtime.get_runtime()
        result.update(available=True, version=client.version)
        account = client.request('account/read', {'refreshToken': False}).get('account')
        result['account'] = public_account(account)
        if account and account.get('type') == 'chatgpt':
            try:
                result['rate_limits'] = client.request('account/rateLimits/read')
            except runtime.CodexError as e:
                result['error'] = str(e)
        elif account:
            result['error'] = '问答运行时只接受 ChatGPT 登录，请重新登录'
        login = client._login_result
        if login and not login.get('success'):
            result['error'] = 'ChatGPT 登录未完成，请重新发起登录'
    except (runtime.CodexError, OSError) as e:
        result['error'] = str(e) if isinstance(e, runtime.CodexError) else '无法访问独立 Codex 配置目录'
    return result


@router.post('/login')
def login():
    try:
        client = runtime.get_runtime()
        client._login_result = None
        result = client.request('account/login/start', {'type': 'chatgpt'})
    except runtime.CodexError as e:
        raise HTTPException(503, str(e)) from e
    except OSError as e:
        raise HTTPException(503, '无法访问独立 Codex 配置目录') from e
    url = result.get('authUrl', '')
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname not in ('auth.openai.com', 'auth.chatgpt.com', 'chatgpt.com'):
        if result.get('loginId'):
            try:
                client.request('account/login/cancel', {'loginId': result['loginId']})
            except runtime.CodexError:
                pass
        raise HTTPException(502, 'Codex 返回了无效的官方登录地址')
    return {'login_id': result['loginId'], 'auth_url': url}


@router.post('/login/cancel')
def cancel_login(body: CancelLogin):
    _request('account/login/cancel', {'loginId': body.login_id})
    return {'ok': True}


@router.post('/logout')
def logout():
    _request('account/logout')
    runtime.close_runtime()
    return {'ok': True}


@router.get('/models')
def models():
    try:
        account = _request('account/read', {'refreshToken': False}).get('account')
        if not account or account.get('type') != 'chatgpt':
            return {'models': [], 'error': '请先登录 ChatGPT'}
        return {'models': runtime.model_choices(), 'error': None}
    except runtime.CodexError as e:
        return {'models': [], 'error': str(e)}
    except HTTPException as e:
        return {'models': [], 'error': e.detail}
