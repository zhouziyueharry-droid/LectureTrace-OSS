"""Read-only real-image acceptance; output stays in a user-specified ignored folder."""
import argparse
import asyncio
import base64
from contextlib import asynccontextmanager
import dataclasses
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import sys
import threading
import time

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette
from starlette.routing import Route
import uvicorn

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'backend'))
from app import config
from app.mcp_server import TokenGate, create_server


def payload(result):
    assert not result.isError, result
    return json.loads(result.content[0].text)


def snapshot(source):
    files, denied = {}, []
    for directory, _, names in os.walk(config.settings.data_dir, onerror=lambda e: denied.append(str(e))):
        for name in names:
            p = Path(directory) / name
            s = p.stat()
            files[str(p)] = (s.st_size, s.st_mtime_ns)
    hashes = {}
    for p in (config.settings.db_path, ROOT / '.env', source):
        with p.open('rb') as f:
            hashes[str(p)] = hashlib.file_digest(f, 'sha256').hexdigest()
    return files, hashes, denied


async def exercise(client, video, image):
    encoded = base64.b64encode(image.read_bytes()).decode()
    start = time.perf_counter()
    located = payload(await client.call_tool('locate_slide', {'video_id': video, 'image_base64': encoded}))
    elapsed = time.perf_counter() - start
    record = {'query': image.name, 'seconds': round(elapsed, 3), 'input_bytes': image.stat().st_size,
              'location': located}
    assert all('text' not in m and 'summary' not in m for m in located['matches'])
    if not located['matches']:
        return record
    match = located['matches'][0]
    args = {'video_id': video, 'version': located['version']}
    preview = payload(await client.call_tool('get_video_units', {**args, 'timestamp': match['timestamp']}))
    chosen = next(u for u in preview['units'] if u['unit_id'] == match['unit_id'])
    assert 'text' not in chosen
    chunks, offset = [], 0
    while offset is not None:
        body = payload(await client.call_tool('read_video_unit', {**args, 'unit_id': chosen['unit_id'], 'text_offset': offset}))
        chunks.append(body['text'])
        offset = body['next_text_offset']
    image_result = await client.call_tool('view_slide', {**args, 'id': chosen['unit_id']})
    assert not image_result.isError and any(c.type == 'image' for c in image_result.content)
    record.update(preview=chosen, transcript=''.join(chunks), image_returned=True)
    return record


async def stdio(args):
    params = StdioServerParameters(command=sys.executable, args=['-m', 'app.mcp_server'],
                                  cwd=str(ROOT / 'backend'), env={'DATA_DIR': str(config.settings.data_dir)})
    async with stdio_client(params) as (rx, tx):
        async with ClientSession(rx, tx, read_timeout_seconds=timedelta(seconds=60)) as client:
            await client.initialize()
            assert len((await client.list_tools()).tools) == 8
            results = []
            for image in [*args.images, args.images[0]]:
                result = await exercise(client, args.video, image)
                results.append(result)
                print(json.dumps({k: result[k] for k in ('query', 'seconds')} | {
                    'matches': len(result['location']['matches']),
                    'transcript_chars': len(result.get('transcript', ''))}), flush=True)
            return results


def http(args):
    original = config.settings
    token = secrets.token_urlsafe(32)
    config.settings = dataclasses.replace(original, mcp_token=token)
    server = create_server()

    @asynccontextmanager
    async def lifespan(app):
        async with server.session_manager.run():
            yield

    app = Starlette(routes=[Route('/mcp', TokenGate(server.streamable_http_app()), methods=['GET', 'POST', 'DELETE'])], lifespan=lifespan)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        runner = uvicorn.Server(uvicorn.Config(app, log_level='error'))
        thread = threading.Thread(target=runner.run, kwargs={'sockets': [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 15
            while not runner.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(.05)
            assert runner.started

            async def run():
                async with httpx.AsyncClient(trust_env=False, headers={'Authorization': 'Bearer ' + token}) as transport:
                    async with streamable_http_client(f'http://127.0.0.1:{port}/mcp', http_client=transport) as (rx, tx, _):
                        async with ClientSession(rx, tx) as client:
                            await client.initialize()
                            return await exercise(client, args.video, args.images[0])
            return asyncio.run(run())
        finally:
            runner.should_exit = True
            thread.join(15)
            config.settings = original
            assert not thread.is_alive()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--video', required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--images', nargs='+', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    before = snapshot(args.source)
    result = {'stdio': asyncio.run(stdio(args)), 'http': http(args)}
    assert snapshot(args.source) == before, 'Course data, settings or source deck changed'
    result.update(data_files_unchanged=len(before[0]), db_env_source_unchanged=True, scan_errors=before[2])
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'saved': str(args.output), 'data_files_unchanged': len(before[0]),
                      'http_matches': len(result['http']['location']['matches'])}), flush=True)
