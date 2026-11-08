"""Standard clients, both transports, synthetic course data; no model requests."""
import asyncio
import base64
import dataclasses
import hashlib
import io
import json
from pathlib import Path
import sys
from datetime import timedelta

import httpx
import pytest
from PIL import Image

pytest.importorskip("mcp")
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette
from starlette.routing import Route

from app import artifacts, config, db
from app.mcp_server import TokenGate, create_server
from test_codex_workspace import VID, course  # reuse the same synthetic evidence as web Q&A


@pytest.fixture
def library(course, tmp_settings, set_settings):
    db.execute("INSERT INTO videos (id,title,filename,created_at,status) VALUES (?,?,?,?,?)",
               (VID, "Synthetic lecture", "example.mp4", db.now_iso(), "done"))
    artifacts.write_artifact(VID, "outline.json", course[1], {})
    set_settings(dataclasses.replace(config.settings, mcp_token="synthetic-test-token"))
    from test_slide_locator import slide
    Image.fromarray(slide()).save(artifacts.video_dir(VID) / 'keyframes/S001.jpg')
    return config.settings.data_dir


def snapshot(root):
    return {str(p.relative_to(root)): (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
            for p in root.rglob("*") if p.is_file()}


def data(result):
    assert not result.isError, result
    parsed = json.loads(result.content[0].text)
    assert parsed == result.structuredContent
    return parsed


async def exercise(session):
    initialized = await session.initialize()
    assert initialized.serverInfo.name == "LectureTrace"
    tools = (await session.list_tools()).tools
    assert {t.name for t in tools} == {"list_videos", "get_video_context", "search_video", "read_evidence", "view_slide",
                                      "get_video_units", "read_video_unit", "locate_slide"}
    assert all(t.annotations.readOnlyHint and not t.annotations.destructiveHint for t in tools)
    assert all(t.inputSchema["type"] == "object" for t in tools)
    assert data(await session.call_tool("list_videos"))["videos"][0]["id"] == VID
    ctx = data(await session.call_tool("get_video_context", {"video_id": VID}))
    assert ctx["timeline"][0]["id"] == "N001"
    args = {"video_id": VID, "version": ctx["version"]}
    from test_slide_locator import slide, encoded
    located = data(await session.call_tool('locate_slide', {'video_id': VID, 'image_base64': encoded(slide())}))
    assert located['matches'][0]['unit_id'] == 'S001.1'
    assert located['version'] == ctx['version'] and located['matches'][0]['timestamp'] == 10
    assert 'summary' not in located['matches'][0] and 'text' not in located['matches'][0]
    preview = data(await session.call_tool('get_video_units', {**args, 'timestamp': 30}))
    assert preview['units'][0]['summary'] == 'Why normalize each mini batch.'
    assert 'Gamma restores' not in str(preview)
    transcript = data(await session.call_tool('read_video_unit', {**args, 'unit_id': 'S001.1'}))
    assert '[T0002]' in transcript['text'] and '[T0003]' in transcript['text']
    assert transcript['next_text_offset'] is None
    search = data(await session.call_tool("search_video", {**args, "queries": ["Gamma"]}))
    assert "T0003" in [r["id"] for r in search["results"]]
    read = data(await session.call_tool("read_evidence", {**args, "ids": ["T0002-T0003"]}))
    assert "Gamma restores" in read["text"] and "[T0002]" in read["text"]
    slide = await session.call_tool("view_slide", {**args, "id": "S001"})
    assert data(slide)["source"] == "slide_image"
    assert slide.content[1].type == "image" and slide.content[1].mimeType == "image/jpeg"
    Image.open(io.BytesIO(base64.b64decode(slide.content[1].data))).verify()
    for name, arguments in [
        ("get_video_context", {"video_id": "../secrets"}),
        ("list_videos", {"offset": -1}),
        ("search_video", {**args, "queries": []}),
        ("read_evidence", {**args, "ids": ["T9999"]}),
        ("view_slide", {**args, "id": "../../secret"}),
        ("read_evidence", {**args, "version": "sha256:" + "0" * 64, "ids": ["T0002"]}),
        ("delete_video", {"video_id": VID}),
        ('get_video_units', {**args, 'timestamp': -1}),
        ('read_video_unit', {**args, 'unit_id': 'S001'}),
        ('locate_slide', {'video_id': VID, 'image_base64': 'not base64'}),
    ]:
        assert (await session.call_tool(name, arguments)).isError


def test_http_standard_client_and_no_writes(library):
    before = snapshot(library)

    async def run():
        server = create_server()
        app = Starlette(routes=[Route("/mcp", TokenGate(server.streamable_http_app()), methods=["GET", "POST", "DELETE"])])
        async with server.session_manager.run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                        headers={"Authorization": "Bearer synthetic-test-token"}) as client:
                async with streamable_http_client("http://127.0.0.1:8000/mcp", http_client=client) as (rx, tx, _):
                    async with ClientSession(rx, tx) as session:
                        await exercise(session)
    asyncio.run(run())
    assert snapshot(library) == before


def test_stdio_standard_client_and_no_writes(library):
    before = snapshot(library)

    async def run():
        params = StdioServerParameters(command=sys.executable, args=["-m", "app.mcp_server"],
                                      cwd=str(Path(__file__).resolve().parents[1]),
                                      env={"DATA_DIR": str(library), "LECTURETRACE_MCP_TOKEN": ""})
        async with stdio_client(params) as (rx, tx):
            async with ClientSession(rx, tx, read_timeout_seconds=timedelta(seconds=20)) as session:
                await exercise(session)
    asyncio.run(run())
    assert snapshot(library) == before


def test_http_auth_and_origin(library, set_settings):
    async def run():
        server = create_server()
        app = TokenGate(server.streamable_http_app())
        async with server.session_manager.run(), httpx.AsyncClient(transport=httpx.ASGITransport(app)) as client:
            url = "http://127.0.0.1:8000/mcp"
            assert (await client.post(url, json={})).status_code == 401
            headers = {"Authorization": "Bearer synthetic-test-token", "Origin": "https://evil.example"}
            assert (await client.post(url, headers=headers, json={})).status_code == 403
            headers.pop("Origin")
            assert (await client.post("http://evil.example/mcp", headers=headers, json={})).status_code == 421
            set_settings(dataclasses.replace(config.settings, mcp_token=""))
            assert (await client.post(url, headers=headers, json={})).status_code == 503
    asyncio.run(run())


def test_pagination_missing_outline_and_path_escape(library, set_settings, tmp_path):
    set_settings(dataclasses.replace(config.settings, mcp=dataclasses.replace(config.settings.mcp, page_size=1, text_chunk_chars=20)))

    async def run():
        server = create_server()
        result = await server.call_tool("get_video_context", {"video_id": VID})
        ctx = data(result)
        assert ctx["next_offset"] == 1 and len(ctx["timeline"]) == 1
        args = {"video_id": VID, "version": ctx["version"], "ids": ["T0002"]}
        first = data(await server.call_tool("read_evidence", args))
        assert len(first["text"]) == 20 and first["next_text_offset"] == 20
        second = data(await server.call_tool("read_evidence", {**args, "text_offset": 20}))
        assert second["text_offset"] == 20
        unit_args = {'video_id': VID, 'version': ctx['version'], 'unit_id': 'S001.1'}
        chunks = []
        offset = 0
        while offset is not None:
            chunk = data(await server.call_tool('read_video_unit', {**unit_args, 'text_offset': offset}))
            chunks.append(chunk['text'])
            offset = chunk['next_text_offset']
        assert 'Gamma restores' in ''.join(chunks) and '[T0002]' in ''.join(chunks)
        (artifacts.video_dir(VID) / "outline.json").unlink()
        assert data(await server.call_tool("get_video_context", {"video_id": VID}))["chapters"] == []
        obj = artifacts.read_json(artifacts.video_dir(VID) / "evidence.json")
        obj["data"]["pages"][0]["image"] = str(tmp_path / "outside.jpg")
        artifacts.write_json(artifacts.video_dir(VID) / "evidence.json", obj)
        Image.new("RGB", (10, 10)).save(tmp_path / "outside.jpg")
        ctx = data(await server.call_tool("get_video_context", {"video_id": VID}))
        result = await server.call_tool("view_slide", {"video_id": VID, "version": ctx["version"], "id": "S001"})
        assert result.isError and str(tmp_path) not in result.content[0].text
    asyncio.run(run())


def test_missing_database_is_not_created(tmp_path, set_settings):
    path = tmp_path / "absent"
    set_settings(dataclasses.replace(config.settings, data_dir=path))
    result = asyncio.run(create_server().call_tool("list_videos", {}))
    assert result.isError and not path.exists()


@pytest.mark.parametrize("version", ["2024-11-05", "2025-03-26", "2025-06-18"])
def test_plain_jsonrpc_client_versions(library, version):
    """No SDK client assumptions: old clients may only consume the content array."""
    async def run():
        server = create_server()
        app = TokenGate(server.streamable_http_app())
        headers = {"Authorization": "Bearer synthetic-test-token", "Accept": "application/json, text/event-stream"}
        async with server.session_manager.run(), httpx.AsyncClient(transport=httpx.ASGITransport(app), headers=headers) as client:
            async def request(method, params):
                response = await client.post("http://127.0.0.1:8000/mcp", json={"jsonrpc": "2.0", "id": 1,
                                                                             "method": method, "params": params})
                assert response.status_code == 200
                return response.json()["result"]
            hello = await request("initialize", {"protocolVersion": version, "capabilities": {},
                                                  "clientInfo": {"name": "plain-jsonrpc", "version": "1"}})
            assert hello["protocolVersion"] == version
            client.headers["MCP-Protocol-Version"] = version
            notification = await client.post("http://127.0.0.1:8000/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
            assert notification.status_code == 202
            assert len((await request("tools/list", {}))["tools"]) == 8
            result = await request("tools/call", {"name": "list_videos", "arguments": {}})
            assert not result.get("isError")
            assert json.loads(result["content"][0]["text"])["videos"][0]["id"] == VID
    asyncio.run(run())
