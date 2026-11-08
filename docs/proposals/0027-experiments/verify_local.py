"""Read-only protocol smoke against existing courses; no model calls or runner."""
import asyncio
from contextlib import asynccontextmanager
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import re
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
sys.path.insert(0, str(ROOT / "backend"))
from app import config
from app.mcp_server import TokenGate, create_server


def snapshot():
    rows = {}
    errors = []
    for directory, _, files in os.walk(config.settings.data_dir, onerror=lambda e: errors.append(str(e))):
        for filename in files:
            p = Path(directory) / filename
            st = p.stat()
            rows[str(p.relative_to(config.settings.data_dir))] = [st.st_size, st.st_mtime_ns]
    digests = {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
               for p in (config.settings.db_path, ROOT / ".env") if p.is_file()}
    return rows, digests, errors


def payload(result):
    assert not result.isError, result
    return json.loads(result.content[0].text)


async def exercise(session):
    hello = await session.initialize()
    tools = (await session.list_tools()).tools
    videos = payload(await session.call_tool("list_videos"))["videos"]
    assert videos
    for video in videos:
        response = await session.call_tool("get_video_context", {"video_id": video["id"]})
        if response.isError:
            continue
        ctx = payload(response)
        section = next((s for s in ctx["timeline"] if s.get("page") and s.get("speech_first")), None)
        if section:
            break
    else:
        raise AssertionError("No prepared video with speech and slides found")
    args = {"video_id": video["id"], "version": ctx["version"]}
    read = payload(await session.call_tool("read_evidence", {**args, "ids": [section["speech_first"]]}))
    words = re.findall(r"[A-Za-z]{4,}", read["text"])
    query = words[0] if words else "课程"
    found = payload(await session.call_tool("search_video", {**args, "queries": [query]}))
    slide = await session.call_tool("view_slide", {**args, "id": section["page"]})
    payload(slide)
    assert any(c.type == "image" and c.mimeType == "image/jpeg" for c in slide.content)
    return {"protocol": hello.protocolVersion, "tools": [t.name for t in tools], "video_count": len(videos),
            "sample_title": video["title"], "text_chars": len(read["text"]),
            "search_hits": len(found["results"]), "image_returned": True}


async def stdio_check():
    params = StdioServerParameters(command=sys.executable, args=["-m", "app.mcp_server"],
                                  cwd=str(ROOT / "backend"), env={"DATA_DIR": str(config.settings.data_dir)})
    async with stdio_client(params) as (rx, tx):
        async with ClientSession(rx, tx) as session:
            return await exercise(session)


def http_check():
    original_settings = config.settings
    token = secrets.token_urlsafe(32)
    config.settings = dataclasses.replace(config.settings, mcp_token=token)
    server = create_server()

    @asynccontextmanager
    async def lifespan(app):
        async with server.session_manager.run():
            yield

    # Test only: ephemeral localhost listener, no application worker or API routes.
    app = Starlette(routes=[Route("/mcp", TokenGate(server.streamable_http_app()), methods=["GET", "POST", "DELETE"])], lifespan=lifespan)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        gateway = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        thread = threading.Thread(target=gateway.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 15
            while not gateway.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(.05)
            assert gateway.started, "Test listener did not start"

            async def run():
                async with httpx.AsyncClient(trust_env=False, headers={"Authorization": "Bearer " + token}) as client:
                    async with streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=client) as (rx, tx, _):
                        async with ClientSession(rx, tx) as session:
                            return await exercise(session)
            return asyncio.run(run())
        finally:
            config.settings = original_settings
            gateway.should_exit = True
            thread.join(timeout=15)
            assert not thread.is_alive()


if __name__ == "__main__":
    before = snapshot()
    result = {"stdio": asyncio.run(stdio_check()), "http": http_check()}
    after = snapshot()
    assert before == after, "Local course files or configuration changed"
    result.update(data_files_unchanged=len(before[0]), db_and_env_unchanged=True, scan_errors=before[2])
    print(json.dumps(result, ensure_ascii=False, indent=2))
