"""字幕 + 抽帧阶段新增功能：幻灯片区域、抽帧展示台接口、设置页 Key、暂未启用的阶段。"""
from __future__ import annotations

import dataclasses
import threading
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import artifacts, config, db, keys
from app.pipeline import keyframes, runner
from app.pipeline.keyframes import crop, detect_slides, valid_roi

from test_api import stage_map, upload, wait_status


@pytest.fixture
def client(tmp_settings, fake_stages):
    from app.main import app
    with TestClient(app) as c:
        yield c


# ---------- 幻灯片区域 ----------

def test_crop_and_validate_roi():
    frame = np.zeros((100, 200), np.uint8)
    assert crop(frame, None).shape == (100, 200)
    assert crop(frame, [0.5, 0.1, 0.5, 0.8]).shape == (80, 100)
    assert valid_roi([0.5, 0.1, 0.5, 0.8])
    assert not valid_roi([0.6, 0, 0.5, 1])      # 超出右边界
    assert not valid_roi([0, 0, 0.01, 1])       # 太窄
    assert not valid_roi([0, 0, 1])


def test_roi_ignores_noisy_camera_half():
    # 左半边是一直在变的摄像头画面，右半边幻灯片不变：不裁剪会切出很多段，裁剪后只有一张
    rng = np.random.default_rng(0)
    slide = np.full((180, 160), 255, np.uint8)
    slide[20:36, 20:140] = 0
    frames = [np.hstack([rng.integers(0, 255, (180, 160), dtype=np.uint8), slide]) for _ in range(12)]
    params = config.KeyframeParams()

    def run(roi):
        samples = ((float(t), crop(f, roi), None) for t, f in enumerate(frames))
        return detect_slides(samples, params, 12.0)

    assert len(run(None)) == 0          # 全画面：每秒都在变，从未稳定
    assert len(run([0.5, 0, 0.5, 1])) == 1


def test_detect_slides_debug_trace():
    a = np.full((90, 160), 255, np.uint8)
    a[60:80, 10:150] = 0                # 页上要有内容：纯白画面会被当作空白段丢弃（交接 §3.5）
    b = a.copy()
    b[10:40, 10:150] = 0
    debug: dict = {}
    detect_slides(((float(t), f, None) for t, f in enumerate([a] * 5 + [b] + [a] * 5)),
                  config.KeyframeParams(), 11.0, debug=debug)
    assert len(debug["trace"]) == 11
    assert debug["short_runs"] == [[5.0, 6.0]]          # 只停留 1 秒的画面被丢弃
    assert [r["kind"] for r in debug["runs"]] == ["new", "merged"]  # 闪一下又回到同一页：合并为同一段


def test_get_roi_prefers_video_then_default(tmp_settings):
    assert keyframes.get_roi("v1") is None
    db.set_setting("keyframe_roi_default", [0, 0, 0.5, 1])
    assert keyframes.get_roi("v1") == [0, 0, 0.5, 1]
    db.set_setting("keyframe_roi:v1", [0.5, 0, 0.5, 1])
    assert keyframes.get_roi("v1") == [0.5, 0, 0.5, 1]


# ---------- 抽帧展示台接口 ----------

def test_keyframes_endpoint_and_roi_rerun(client, fake_stages):
    vid = upload(client)
    wait_status(client, vid, "awaiting_confirm")
    artifacts.write_artifact(vid, "keyframes.json", [{"id": "S001", "image": "keyframes/S001.jpg",
                                                      "appearances": [[0, 5]]}], {"interval_s": 1})
    artifacts.write_artifact(vid, "keyframes_debug.json", {"trace": [[0, 0, 0]], "roi": None}, {})
    r = client.get(f"/api/videos/{vid}/keyframes").json()
    assert r["slides"][0]["id"] == "S001" and r["debug"]["trace"] == [[0, 0, 0]]
    assert r["roi"] is None and r["params"] == {"interval_s": 1}

    bad = client.put(f"/api/videos/{vid}/keyframes/roi", json={"roi": [0.9, 0, 0.5, 1]})
    assert bad.status_code == 400

    r = client.put(f"/api/videos/{vid}/keyframes/roi",
                   json={"roi": [0.5, 0.1, 0.5, 0.8], "as_default": True, "rerun": True})
    assert r.status_code == 200
    v = wait_status(client, vid, "awaiting_confirm")
    assert fake_stages.calls.count("keyframes") == 2
    assert stage_map(v)["keyframes"]["status"] == "done"
    assert keyframes.get_roi(vid) == [0.5, 0.1, 0.5, 0.8]
    assert db.get_setting("keyframe_roi_default") == [0.5, 0.1, 0.5, 0.8]

    client.delete(f"/api/videos/{vid}")
    assert db.get_setting(f"keyframe_roi:{vid}") is None


def test_roi_rerun_runs_keyframes_while_asr_is_busy(client, fake_stages):
    # 语音识别卡住（主队列忙）时，重新抽帧在独立线程里立即执行，不等语音识别
    release = threading.Event()
    in_asr = threading.Event()
    fake_stages.hooks["asr"] = lambda ctx: (in_asr.set(), release.wait(10))
    vid = upload(client)
    assert in_asr.wait(5)
    # 第一次抽帧与语音识别同时开始：先等它结束，否则修改区域会得到 409「正在抽帧」（原偶发失败的原因）
    deadline = time.monotonic() + 5
    while ((vid, "keyframes") in runner._side_jobs
           or stage_map(client.get(f"/api/videos/{vid}").json())["keyframes"]["status"] != "done") \
            and time.monotonic() < deadline:
        time.sleep(0.02)
    assert fake_stages.calls.count("keyframes") == 1
    r = client.put(f"/api/videos/{vid}/keyframes/roi", json={"roi": [0.5, 0, 0.5, 1]})
    assert r.status_code == 200
    deadline = time.monotonic() + 5
    while fake_stages.calls.count("keyframes") < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert fake_stages.calls.count("keyframes") == 2      # asr 还没结束，抽帧已重跑
    assert client.get(f"/api/videos/{vid}").json()["status"] == "processing"
    release.set()
    v = wait_status(client, vid, "awaiting_confirm")
    assert stage_map(v)["keyframes"]["status"] == "done"
    assert fake_stages.calls.count("keyframes") == 2      # 主队列不会再跑一次


def test_frame_endpoint_rejects_missing_video(client):
    assert client.get("/api/videos/000000000000/frame?t=1").status_code == 404


# ---------- 暂未启用的阶段 ----------

def test_disabled_stages_are_skipped(client, fake_stages, set_settings):
    set_settings(dataclasses.replace(config.settings, disabled_stages=("read_slides", "evidence", "outline")))
    db.set_setting("skip_confirm", True)
    vid = upload(client)
    v = wait_status(client, vid, "done")
    st = stage_map(v)
    for name in ("read_slides", "evidence", "outline"):
        assert st[name]["status"] == "skipped" and st[name]["error"] == runner.DISABLED_REASON
    assert "outline" not in fake_stages.calls and "translate" in fake_stages.calls

    # 重跑抽帧只连带重置未启用的外部阶段时，不需要重新确认发送
    db.set_setting("skip_confirm", False)
    targets = runner.reset_stages(vid, ["keyframes"])
    assert runner.AWAIT not in targets

    # 重新启用后，之前因未启用而跳过的阶段会被执行
    set_settings(dataclasses.replace(config.settings, disabled_stages=()))
    db.set_setting("skip_confirm", True)
    runner.enqueue(vid)
    v = wait_status(client, vid, "done")
    assert stage_map(v)["outline"]["status"] == "done" and "outline" in fake_stages.calls


# ---------- 设置页 Key ----------

def test_settings_keys_masked_kept_and_cleared(client):
    r = client.get("/api/settings").json()["keys"]
    assert r["deepl"]["has_key"] is False and r["openai"]["base_url"] == keys.DEFAULT_OPENAI_BASE_URL

    r = client.put("/api/settings", json={"deepl": "secret-1234:fx", "openai": "sk-abcd9876"}).json()["keys"]
    assert r["deepl"] == {"has_key": True, "key_hint": "…4:fx", "source": "settings"}
    assert "secret" not in str(r) and r["openai"]["key_hint"] == "…9876"

    # 缺省表示保留，"" 表示清除
    r = client.put("/api/settings", json={"openai": ""}).json()["keys"]
    assert r["deepl"]["has_key"] is True and r["openai"]["has_key"] is False
    assert keys.get_key("deepl") == "secret-1234:fx"

    assert client.put("/api/settings", json={"openai_base_url": "ftp://x"}).status_code == 400
    r = client.put("/api/settings", json={"openai_base_url": "https://api.deepseek.com"}).json()["keys"]
    assert r["openai"]["base_url"] == "https://api.deepseek.com"


def test_settings_test_without_key(client):
    r = client.post("/api/settings/test", json={"service": "openai"}).json()
    assert r == {"ok": False, "message": "尚未填写 API Key", "latency_ms": 0}
    assert client.post("/api/settings/test", json={"service": "x"}).status_code == 400


def test_confirm_blocked_without_deepl_key(client, set_settings):
    set_settings(dataclasses.replace(config.settings, translate=config.TranslateParams(provider="deepl")))
    vid = upload(client)
    wait_status(client, vid, "awaiting_confirm")
    r = client.post(f"/api/videos/{vid}/confirm", json={})
    assert r.status_code == 409 and "DeepL API Key" in r.json()["detail"]


# ---------- 抽帧与语音识别并行 ----------

def test_keyframes_runs_concurrently_with_asr(client, fake_stages):
    # 抽帧要等到语音识别开始后才结束：如果两者仍按顺序执行，这里会超时
    asr_started = threading.Event()
    overlapped = []
    fake_stages.hooks["asr"] = lambda ctx: asr_started.set()
    fake_stages.hooks["keyframes"] = lambda ctx: overlapped.append(asr_started.wait(5))
    vid = upload(client)
    v = wait_status(client, vid, "awaiting_confirm")
    assert overlapped == [True]
    assert all(stage_map(v)[n]["status"] == "done" for n in ("keyframes", "asr", "cues"))


def test_keyframes_failure_in_parallel_fails_video(client, fake_stages):
    from app.pipeline.base import StageError

    fake_stages.fail["keyframes"] = StageError("OpenCV 无法打开视频文件")
    vid = upload(client)
    v = wait_status(client, vid, "failed")
    st = stage_map(v)
    assert st["keyframes"]["status"] == "failed" and "OpenCV" in st["keyframes"]["error"]
    assert st["cues"]["status"] == "done"             # 语音识别一侧照常完成
    assert st["await_confirm"]["status"] == "pending"  # 不会进入等待确认

    client.post(f"/api/videos/{vid}/retry", json={})   # 重试只补跑失败的抽帧
    v = wait_status(client, vid, "awaiting_confirm")
    assert fake_stages.calls.count("keyframes") == 2 and fake_stages.calls.count("asr") == 1


def test_frontend_entry_not_cached(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from app import main

    dist = tmp_path / "frontend-dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>LectureTrace</html>", encoding="utf-8")
    (dist / "assets" / "app-abc123.js").write_text("console.log('test')", encoding="utf-8")
    monkeypatch.setattr(main, "FRONTEND_DIST", dist)
    monkeypatch.setattr(main, "app", FastAPI())
    main._mount_frontend()
    client = TestClient(main.app)
    for url in ("/", "/videos/abc", "/index.html"):
        assert client.get(url).headers["cache-control"] == "no-cache", url
    asset = next((main.FRONTEND_DIST / "assets").glob("*.js")).name
    assert "immutable" in client.get(f"/assets/{asset}").headers["cache-control"]
