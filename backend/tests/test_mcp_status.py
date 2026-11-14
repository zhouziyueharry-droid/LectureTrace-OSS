"""Settings probes use the real local MCP stack and synthetic course data only."""
import asyncio
from dataclasses import replace

import httpx
import pytest
from fastapi import FastAPI

pytest.importorskip('mcp')
from app.api import mcp_status
from app.mcp_server import McpHTTP


async def check(service):
    app = FastAPI()
    app.state.mcp_http = service
    app.include_router(mcp_status.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
        response = await client.get('/api/mcp/status')
        assert response.headers['cache-control'] == 'no-store'
        assert response.status_code == 200
        return response.json()


def test_unavailable_states(tmp_settings, set_settings):
    set_settings(replace(tmp_settings, mcp_token=''))
    assert asyncio.run(check(None))['state'] == 'missing_dependency'
    assert asyncio.run(check(McpHTTP()))['state'] == 'disabled'
    set_settings(replace(tmp_settings, mcp_token='test-secret'))
    assert asyncio.run(check(McpHTTP()))['state'] == 'starting'


def test_protocol_tools_and_empty_library(tmp_settings, set_settings):
    set_settings(replace(tmp_settings, mcp_token='test-secret-never-return'))
    before = tmp_settings.db_path.read_bytes()

    async def run():
        service = McpHTTP()
        async with service.lifespan():
            result = await check(service)
        assert result['state'] == 'ready'
        assert result['library_readable'] is True
        assert len(result['tools']) == 10
        assert 'read_video_transcript' in [tool['name'] for tool in result['tools']]
        assert result['protocol_version']
        assert 'test-secret' not in str(result)
    asyncio.run(run())
    assert tmp_settings.db_path.read_bytes() == before


def test_disabled_http_still_checks_local_tools_and_library(tmp_settings, set_settings):
    set_settings(replace(tmp_settings, mcp_token=''))
    async def run():
        service = McpHTTP()
        async with service.lifespan():
            result = await check(service)
        assert result['state'] == 'disabled'
        assert result['library_readable'] is True
        assert len(result['tools']) == 10
        assert result['protocol_version'] is None
    asyncio.run(run())


def test_library_failure_does_not_hide_protocol_success(tmp_settings, set_settings, monkeypatch):
    from app import mcp_server
    set_settings(replace(tmp_settings, mcp_token='test-secret'))
    def broken(*args):
        raise RuntimeError('private path and test-secret')
    monkeypatch.setattr(mcp_server, '_rows', broken)

    async def run():
        service = McpHTTP()
        async with service.lifespan():
            result = await check(service)
        assert result['state'] == 'error'
        assert result['error_code'] == 'library_unreadable'
        assert result['library_readable'] is False
        assert len(result['tools']) == 10
        assert 'private path' not in str(result)
        assert 'test-secret' not in str(result)
    asyncio.run(run())


def test_probe_timeout_and_protocol_error(tmp_settings, set_settings, monkeypatch):
    set_settings(replace(tmp_settings, mcp_token='test-secret'))
    monkeypatch.setattr(mcp_status, 'CHECK_TIMEOUT', 0.02)

    class Slow:
        app = True
        async def __call__(self, scope, receive, send):
            await asyncio.sleep(10)

    assert asyncio.run(check(Slow()))['error_code'] == 'timeout'

    class Broken:
        app = True
        async def __call__(self, scope, receive, send):
            raise RuntimeError('test-secret')

    result = asyncio.run(check(Broken()))
    assert result['state'] == 'error'
    assert result['error_code'] == 'protocol_failed'
    assert 'test-secret' not in str(result)
