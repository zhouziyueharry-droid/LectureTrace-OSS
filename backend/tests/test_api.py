"""HTTP 接口测试：TestClient + 假阶段模块。"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
import types

import pytest
from fastapi.testclient import TestClient

from app import artifacts, db, events
from app.pipeline import runner
from app.pipeline.base import LOCAL_STAGES, STAGES, StageError

VIDEO_BYTES = bytes(range(256)) * 40   # 10240 字节


@pytest.fixture
def client(tmp_settings, fake_stages):
    from app.main import app
    with TestClient(app) as c:
        yield c


def upload(client, name="Lecture 1.mp4", data=VIDEO_BYTES) -> str:
    r = client.post("/api/videos", files={"file": (name, data, "video/mp4")})
    assert r.status_code == 200, r.text
    return r.json()["video_id"]


def wait_status(client, vid, *statuses, timeout=10) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = client.get(f"/api/videos/{vid}").json()
        if v["status"] in statuses and runner.wait_idle(0.05):
            return v
        time.sleep(0.02)
    raise AssertionError(f"等待 {statuses} 超时：{v['status']}")


def stage_map(v: dict) -> dict:
    return {s["name"]: s for s in v["stages"]}


def test_full_flow(client, fake_stages):
    vid = upload(client)
    assert len(vid) == 12
    assert (artifacts.video_dir(vid) / "source.mp4").read_bytes() == VIDEO_BYTES

    v = wait_status(client, vid, "awaiting_confirm")
    assert v["title"] == "Lecture 1" and v["filename"] == "Lecture 1.mp4"
    assert v["duration_s"] == 12.5
    st = stage_map(v)
    assert [s["name"] for s in v["stages"]] == STAGES
    assert all(st[n]["status"] == "done" for n in LOCAL_STAGES)
    assert st["await_confirm"]["status"] == "running"
    assert v["stale_stages"] == []

    lst = client.get("/api/videos").json()
    assert [x["id"] for x in lst] == [vid]
    assert set(lst[0]) == {"id", "title", "filename", "duration_s", "created_at", "status", "course_id"}
    assert lst[0]["course_id"] is None

    # 大纲未生成
    r = client.get(f"/api/videos/{vid}/outline")
    assert r.status_code == 404 and r.json()["detail"] == "大纲尚未生成"

    r = client.post(f"/api/videos/{vid}/confirm", json={"remember": False})
    assert r.status_code == 200
    v = wait_status(client, vid, "done")
    assert all(s["status"] == "done" for s in v["stages"])

    outline = client.get(f"/api/videos/{vid}/outline").json()
    assert outline["chapters"][0]["start"] == 0.0
    assert client.get(f"/api/videos/{vid}/slides").json()[0]["id"] == "S001"

    r = client.get(f"/api/videos/{vid}/subtitles/bilingual.vtt")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/vtt")
    assert "嗨。" in r.text
    assert client.get(f"/api/videos/{vid}/subtitles/source.vtt").status_code == 200
    assert client.get(f"/api/videos/{vid}/subtitles/other.vtt").status_code == 404

    r = client.get(f"/api/videos/{vid}/slides/S001.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"


def test_upload_rejects_non_mp4(client, tmp_settings):
    r = client.post("/api/videos", files={"file": ("notes.pdf", b"%PDF", "application/pdf")})
    assert r.status_code == 400 and r.json()["detail"] == "只支持 MP4 文件"
    r = client.post("/api/videos", data={"x": "1"})
    assert r.status_code == 400
    assert client.get("/api/videos").json() == []
    vdir = tmp_settings.videos_dir
    assert not vdir.exists() or not any(vdir.iterdir())   # 失败的上传不留目录


def test_range_request(client):
    vid = upload(client)
    url = f"/api/videos/{vid}/file"
    r = client.get(url)
    assert r.status_code == 200 and r.content == VIDEO_BYTES
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["content-type"] == "video/mp4"

    r = client.get(url, headers={"Range": "bytes=100-199"})
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes 100-199/{len(VIDEO_BYTES)}"
    assert r.content == VIDEO_BYTES[100:200]

    r = client.get(url, headers={"Range": "bytes=10000-"})
    assert r.status_code == 206 and r.content == VIDEO_BYTES[10000:]

    r = client.get(url, headers={"Range": "bytes=-16"})
    assert r.status_code == 206 and r.content == VIDEO_BYTES[-16:]

    r = client.get(url, headers={"Range": "bytes=999999-"})
    assert r.status_code == 416
    wait_status(client, vid, "awaiting_confirm")


def test_failure_then_retry(client, fake_stages):
    fake_stages.fail["translate"] = StageError("翻译失败：未配置 ANTHROPIC_API_KEY")
    vid = upload(client)
    wait_status(client, vid, "awaiting_confirm")
    client.post(f"/api/videos/{vid}/confirm", json={})
    v = wait_status(client, vid, "failed")
    st = stage_map(v)
    assert st["translate"]["status"] == "failed"
    assert st["translate"]["error"] == "翻译失败：未配置 ANTHROPIC_API_KEY"
    assert (artifacts.video_dir(vid) / "keyframes.json").exists()

    fake_stages.calls.clear()
    r = client.post(f"/api/videos/{vid}/retry")
    assert r.status_code == 200
    v = wait_status(client, vid, "done")
    assert fake_stages.calls == ["translate", "read_slides", "evidence", "outline"]
    assert stage_map(v)["translate"]["attempt"] == 2
    assert stage_map(v)["cues"]["attempt"] == 1


def test_confirm_not_waiting_and_remember(client):
    vid = upload(client)
    wait_status(client, vid, "awaiting_confirm")
    r = client.post(f"/api/videos/{vid}/confirm", json={"remember": True})
    assert r.status_code == 200
    wait_status(client, vid, "done")
    r = client.post(f"/api/videos/{vid}/confirm", json={"remember": True})
    assert r.status_code == 409 and r.json()["detail"] == "当前不在等待确认状态"

    # skip_confirm 已记住：没有 GPT 阶段要跑时新视频不再停下
    db.set_setting("llm_settings", {"outline_enabled": False})
    vid2 = upload(client, "b.MP4")
    v = wait_status(client, vid2, "done")
    assert stage_map(v)["await_confirm"]["status"] == "skipped"


def test_retry_stale_stages(client, set_settings):
    import dataclasses
    from app import config
    vid = upload(client)
    wait_status(client, vid, "awaiting_confirm")
    client.post(f"/api/videos/{vid}/confirm", json={})
    wait_status(client, vid, "done")
    set_settings(dataclasses.replace(config.settings, llm_model="other"))
    v = client.get(f"/api/videos/{vid}").json()
    assert v["stale_stages"] == ["translate", "read_slides", "evidence", "outline"]
    r = client.post(f"/api/videos/{vid}/retry", json={"stages": v["stale_stages"]})
    assert r.status_code == 200
    wait_status(client, vid, "awaiting_confirm")   # 重跑会调用 GPT，先确认
    client.post(f"/api/videos/{vid}/confirm", json={})
    v = wait_status(client, vid, "done")
    assert v["stale_stages"] == []
    assert client.post(f"/api/videos/{vid}/retry", json={"stages": ["bad"]}).status_code == 400


def test_invalid_ids_and_traversal(client):
    assert client.get("/api/videos/nope").status_code == 404
    assert client.get("/api/videos/..%2F..%2Fapp.db").status_code == 404
    assert client.get("/api/videos/000000000000").json() == {"detail": "视频不存在"}
    vid = upload(client)
    wait_status(client, vid, "awaiting_confirm")
    for path in ["slides/..%2Fsource.jpg", "slides/S001%2F..%2F..%2Fsource.jpg", "slides/x.jpg",
                 "subtitles/..%2Fsource.vtt"]:
        r = client.get(f"/api/videos/{vid}/{path}")
        assert r.status_code == 404, path


def test_delete(client):
    vid = upload(client)
    wait_status(client, vid, "awaiting_confirm")
    assert client.delete(f"/api/videos/{vid}").json() == {"ok": True}
    assert client.get(f"/api/videos/{vid}").status_code == 404
    assert not artifacts.video_dir(vid).exists()
    assert db.fetch_all("SELECT * FROM stages WHERE video_id = ?", (vid,)) == []


def test_delete_while_running(client, fake_stages):
    started, release = threading.Event(), threading.Event()

    def block(ctx):
        started.set()
        release.wait(5)

    fake_stages.hooks["asr"] = block
    vid = upload(client)
    assert started.wait(5)
    assert client.delete(f"/api/videos/{vid}").status_code == 200
    release.set()
    assert runner.wait_idle(5)
    assert sorted(fake_stages.calls) == ["asr", "extract_audio", "keyframes"]   # 取消后不再继续（抽帧在独立线程，顺序不定）
    assert not artifacts.video_dir(vid).exists()
    assert client.get("/api/videos").json() == []


def test_send_plan(client, monkeypatch):
    mod = types.ModuleType("app.pipeline.send_plan")
    mod.build_send_plan = lambda vid: {"provider": "Anthropic (x)", "text_chars": 3, "slide_count": 1,
                                       "estimated_cost_usd": 0.01, "translator": "LLM", "items": []}
    monkeypatch.setitem(sys.modules, "app.pipeline.send_plan", mod)
    vid = upload(client)
    wait_status(client, vid, "awaiting_confirm")
    assert client.get(f"/api/videos/{vid}/send-plan").json()["slide_count"] == 1


def test_chat(client, monkeypatch):
    seen_threads = []

    def ask(video_id, question):
        seen_threads.append(threading.current_thread().name)
        yield {"type": "delta", "text": "答案"}
        yield {"type": "done", "message": {"id": 1, "role": "assistant", "content": "答案 [S001]",
                                           "citations": [{"id": "S001", "start": 0.0}], "created_at": "x"}}

    mod = types.ModuleType("app.qa.chat")
    mod.ask = ask
    mod.history = lambda vid: [{"id": 1, "role": "user", "content": "q", "citations": [], "created_at": "x"}]
    monkeypatch.setitem(sys.modules, "app.qa.chat", mod)

    vid = upload(client)
    wait_status(client, vid, "awaiting_confirm")
    assert client.get(f"/api/videos/{vid}/chat").json()[0]["content"] == "q"

    r = client.post(f"/api/videos/{vid}/chat", json={"question": "什么是 BN？"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    items = [json.loads(line[5:]) for line in r.text.splitlines() if line.startswith("data:")]
    assert [i["type"] for i in items] == ["delta", "done"]
    assert items[1]["message"]["citations"][0]["id"] == "S001"
    assert seen_threads and seen_threads[0] != threading.main_thread().name

    assert client.post(f"/api/videos/{vid}/chat", json={"question": "  "}).status_code == 400
    deleted = client.delete(f"/api/videos/{vid}/chat").json()
    assert len(deleted["conversations"]) == 1 and deleted["current_id"] == deleted["conversations"][0]["id"]


def test_events_snapshot_then_live(client):
    """直接驱动 SSE 响应的 body_iterator：先 snapshot，再收到工作线程发布的事件。"""
    from app.api import processing
    vid = upload(client)
    wait_status(client, vid, "awaiting_confirm")

    async def run():
        resp = await processing.video_events(vid)
        it = resp.body_iterator
        first = json.loads((await it.__anext__())["data"])
        # 从另一个线程发布，模拟工作线程
        threading.Thread(target=events.publish,
                         args=(vid, {"type": "video", "status": "processing"})).start()
        second = json.loads((await asyncio.wait_for(it.__anext__(), 5))["data"])
        await it.aclose()
        return first, second

    first, second = asyncio.run(run())
    assert first["type"] == "snapshot" and first["video"]["id"] == vid
    assert stage_map(first["video"])["await_confirm"]["status"] == "running"
    assert second == {"type": "video", "status": "processing"}
    assert events.subscriber_count(vid) == 0


def test_validation_error_format(client):
    vid = upload(client)
    wait_status(client, vid, "awaiting_confirm")
    r = client.post(f"/api/videos/{vid}/chat", json={})
    assert r.status_code == 422 and isinstance(r.json()["detail"], str)
