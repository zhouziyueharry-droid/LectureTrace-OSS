"""Read-only diagnostics for the mounted local MCP service, never an arbitrary URL."""
from datetime import datetime, timezone
import time

import anyio
import httpx
from fastapi import APIRouter, Request, Response

from .. import config

router = APIRouter(prefix='/api/mcp', tags=['mcp'])
CHECK_TIMEOUT = 5


@router.get('/status')
async def get_status(request: Request, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    started = time.monotonic()
    service = getattr(request.app.state, 'mcp_http', None)
    token = config.settings.mcp_token
    result = dict(state='starting', token_configured=bool(token), endpoint='/mcp',
                  tools=[], library_readable=None, protocol_version=None, error_code=None,
                  checked_at=datetime.now(timezone.utc).isoformat(), latency_ms=0)
    if service is None:
        result['state'] = 'missing_dependency'
    elif not token:
        result['state'] = 'disabled'
        if service.server is not None:
            try:
                with anyio.fail_after(CHECK_TIMEOUT):
                    tools = await service.server.list_tools()
                    result['tools'] = [{'name': tool.name, 'description': tool.description or ''} for tool in tools]
                    library = await service.server.call_tool('list_videos', {})
                    result['library_readable'] = not library.isError
                    if library.isError:
                        result.update(state='error', error_code='library_unreadable')
            except TimeoutError:
                result.update(state='error', error_code='timeout')
            except Exception:
                result.update(state='error', error_code='protocol_failed')
    elif service.app is not None:
        try:
            # Exercise the same token gate and protocol stack without exposing a token or trusting a URL.
            with anyio.fail_after(CHECK_TIMEOUT):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(service),
                        base_url='http://127.0.0.1:8000',
                        headers={'Authorization': 'Bearer ' + token,
                                 'Accept': 'application/json, text/event-stream'}) as client:
                    async def rpc(method, params):
                        reply = await client.post('/mcp', json={
                            'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})
                        reply.raise_for_status()
                        return reply.json()['result']

                    hello = await rpc('initialize', {'protocolVersion': '2025-06-18',
                        'capabilities': {}, 'clientInfo': {'name': 'LectureTrace status', 'version': '1'}})
                    result['protocol_version'] = hello['protocolVersion']
                    client.headers['MCP-Protocol-Version'] = hello['protocolVersion']
                    initialized = await client.post('/mcp', json={
                        'jsonrpc': '2.0', 'method': 'notifications/initialized'})
                    initialized.raise_for_status()
                    tools = await rpc('tools/list', {})
                    result['tools'] = [{'name': tool['name'], 'description': tool.get('description', '')}
                                       for tool in tools['tools']]
                    library = await rpc('tools/call', {'name': 'list_videos', 'arguments': {}})
                    result['library_readable'] = not library.get('isError', False)
                    result['state'] = 'ready' if result['library_readable'] else 'error'
                    if not result['library_readable']:
                        result['error_code'] = 'library_unreadable'
        except TimeoutError:
            result.update(state='error', error_code='timeout')
        except Exception:
            # Return stable codes, never provider errors, credentials, paths or course metadata.
            result.update(state='error', error_code='protocol_failed')
    result['latency_ms'] = round((time.monotonic() - started) * 1000)
    return result
