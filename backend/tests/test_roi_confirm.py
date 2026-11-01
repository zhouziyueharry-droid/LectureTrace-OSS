"""幻灯片区域（ROI）确认流程（交接 0001 §2.5「验收」）。

用 manual_roi 关闭 conftest 的自动确认；竞争时序用 runner._hooks 在关键点卡住线程构造，不靠 sleep。
"""
from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from app import artifacts, db, events
from app.pipeline import keyframes, runner
from app.pipeline import roi as roi_store
from app.pipeline.base import StageError

from test_api import stage_map, upload, wait_status


@pytest.fixture
def client(tmp_settings, fake_stages):
    from app.main import app
    with TestClient(app) as c:
        yield c


def wait_until(cond, timeout: float = 5.0, msg: str = "条件未满足") -> None:
    """轮询一个确定的条件（不是靠等待时长碰运气）。"""
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError(msg)
        time.sleep(0.01)


def detail(client, vid) -> dict:
    return client.get(f"/api/videos/{vid}").json()


def put_roi(client, vid, **body):
    return client.put(f"/api/videos/{vid}/keyframes/roi", json=body)


# ---------- 基本流程 ----------

def test_unconfirmed_waits_at_awaiting_roi_then_confirm_continues(client, fake_stages, manual_roi):
    vid = upload(client)
    with events.subscribe_sync(vid) as q:
        v = wait_status(client, vid, "awaiting_roi")
        evs = [q.get() for _ in range(q.qsize())]
    st = stage_map(v)
    # 语音识别照常，抽帧不开始，停在汇合点之前
    assert all(st[n]["status"] == "done" for n in ("extract_audio", "asr", "cues"))
    assert st["keyframes"]["status"] == "pending" and st["segments"]["status"] == "pending"
    assert "keyframes" not in fake_stages.calls and "segments" not in fake_stages.calls
    assert vid in runner._parked
    assert any(e.get("stage") == "keyframes" and e.get("message") == runner.ROI_WAIT_MESSAGE for e in evs)
    assert v["roi_confirmed"] is False and v["roi"] is None and v["roi_suggestion"] is None

    r = put_roi(client, vid, roi=[0.5, 0, 0.5, 1])
    assert r.status_code == 200 and r.json()["roi_confirmed"] is True
    v = wait_status(client, vid, "awaiting_confirm")
    assert fake_stages.calls.count("keyframes") == 1 and fake_stages.calls.count("asr") == 1
    assert stage_map(v)["segments"]["status"] == "done"
    assert vid not in runner._parked
    assert v["roi"] == [0.5, 0, 0.5, 1] and v["roi_suggestion"] == [0.5, 0, 0.5, 1]
    rec = db.get_setting(roi_store.confirmed_key(vid))
    assert rec["roi"] == [0.5, 0, 0.5, 1] and rec["confirmed_at"]


def test_confirm_before_asr_starts(client, fake_stages, manual_roi):
    in_audio, release = threading.Event(), threading.Event()
    fake_stages.hooks["extract_audio"] = lambda ctx: (in_audio.set(), release.wait(10))
    vid = upload(client)
    assert in_audio.wait(5)
    assert put_roi(client, vid, full_frame=True).status_code == 200
    # 抽帧立即在独立线程开始，不等主循环
    wait_until(lambda: stage_map(detail(client, vid))["keyframes"]["status"] == "done", msg="抽帧没有立即开始")
    assert "asr" not in fake_stages.calls
    release.set()
    v = wait_status(client, vid, "awaiting_confirm")
    assert fake_stages.calls.count("keyframes") == 1
    assert v["roi_confirmed"] is True and v["roi"] is None   # 整个画面
    assert db.get_setting(roi_store.confirmed_key(vid))["roi"] == [0, 0, 1, 1]


def test_confirm_while_asr_running_then_segments_waits_for_keyframes(client, fake_stages, manual_roi):
    in_asr, asr_release = threading.Event(), threading.Event()
    in_kf, kf_release = threading.Event(), threading.Event()
    fake_stages.hooks["asr"] = lambda ctx: (in_asr.set(), asr_release.wait(10))
    fake_stages.hooks["keyframes"] = lambda ctx: (in_kf.set(), kf_release.wait(10))
    vid = upload(client)
    assert in_asr.wait(5)
    assert put_roi(client, vid, roi=[0, 0, 0.5, 1]).status_code == 200
    assert in_kf.wait(5)                                  # 抽帧已在独立线程开始，语音识别仍在进行
    assert detail(client, vid)["status"] == "processing"
    asr_release.set()
    # 语音识别、字幕完成后，主循环在 segments 前等待抽帧
    wait_until(lambda: stage_map(detail(client, vid))["cues"]["status"] == "done")
    assert "segments" not in fake_stages.calls and vid not in runner._parked
    assert detail(client, vid)["status"] == "processing"
    kf_release.set()
    v = wait_status(client, vid, "awaiting_confirm")
    assert fake_stages.calls.count("keyframes") == 1
    assert fake_stages.calls.index("keyframes") < fake_stages.calls.index("segments")
    assert stage_map(v)["segments"]["status"] == "done"


# ---------- 竞争时序 ----------

def test_race_keyframes_finish_after_gate_parked(client, fake_stages, manual_roi):
    """主循环在 _keyframes_gate 判定"未确认"之后、离开之前，抽帧已经跑完：仍会被叫回。"""
    parked, release = threading.Event(), threading.Event()

    def hold(video_id):
        parked.set()
        release.wait(10)

    runner._hooks["gate_parked"] = hold
    vid = upload(client)
    assert parked.wait(5)
    assert vid in runner._parked
    assert put_roi(client, vid, roi=[0.5, 0, 0.5, 1]).status_code == 200
    # 抽帧结束时在锁内看到 _parked，把视频叫回（主循环还没离开，走 _rerun）
    wait_until(lambda: vid not in runner._parked and (vid, "keyframes") not in runner._side_jobs,
               msg="抽帧结束时没有叫回视频")
    assert fake_stages.calls.count("keyframes") == 1
    with runner._lock:
        assert vid in runner._rerun or vid in runner._queued
    release.set()
    v = wait_status(client, vid, "awaiting_confirm")
    assert stage_map(v)["segments"]["status"] == "done"
    assert fake_stages.calls.count("keyframes") == 1 and fake_stages.calls.count("asr") == 1
    assert vid not in runner._parked


def test_race_gate_reached_after_record_before_start(client, fake_stages, manual_roi):
    """确认记录已写入、_start_side 尚未调用时，主循环到达汇合点：主循环自己启动抽帧，不会重复。"""
    in_asr, asr_release = threading.Event(), threading.Event()
    recorded, confirm_release = threading.Event(), threading.Event()
    fake_stages.hooks["asr"] = lambda ctx: (in_asr.set(), asr_release.wait(10))

    def hold(video_id):
        recorded.set()
        confirm_release.wait(10)

    runner._hooks["roi_recorded"] = hold
    vid = upload(client)
    assert in_asr.wait(5)
    result = {}
    t = threading.Thread(target=lambda: result.update(r=runner.confirm_roi(vid, [0, 0, 0.5, 1])))
    t.start()
    assert recorded.wait(5)
    asr_release.set()
    v = wait_status(client, vid, "awaiting_confirm")      # 确认线程仍卡在 _start_side 之前
    assert fake_stages.calls.count("keyframes") == 1 and stage_map(v)["segments"]["status"] == "done"
    confirm_release.set()
    t.join(5)
    assert result["r"] == "started"
    assert runner.wait_idle(5)
    assert fake_stages.calls.count("keyframes") == 1      # 抽帧已完成，确认线程不会再跑一遍
    assert vid not in runner._parked


# ---------- 失败、重启、删除 ----------

def test_keyframes_failure_after_confirm_fails_video(client, fake_stages, manual_roi):
    vid = upload(client)
    wait_status(client, vid, "awaiting_roi")
    fake_stages.fail["keyframes"] = StageError("OpenCV 无法打开视频文件")
    assert put_roi(client, vid, full_frame=True).status_code == 200
    v = wait_status(client, vid, "failed")
    assert stage_map(v)["keyframes"]["status"] == "failed" and stage_map(v)["segments"]["status"] == "pending"
    assert vid not in runner._parked

    # 重试：已确认，主循环直接补跑抽帧
    client.post(f"/api/videos/{vid}/retry", json={})
    wait_status(client, vid, "awaiting_confirm")
    assert fake_stages.calls.count("keyframes") == 2 and fake_stages.calls.count("asr") == 1


def test_keyframes_failure_while_asr_running_fails_video(client, fake_stages, manual_roi):
    in_asr, release = threading.Event(), threading.Event()
    fake_stages.hooks["asr"] = lambda ctx: (in_asr.set(), release.wait(10))
    fake_stages.fail["keyframes"] = StageError("抽帧失败")
    vid = upload(client)
    assert in_asr.wait(5)
    put_roi(client, vid, full_frame=True)
    wait_until(lambda: stage_map(detail(client, vid))["keyframes"]["status"] == "failed")
    release.set()
    v = wait_status(client, vid, "failed")
    assert stage_map(v)["cues"]["status"] == "done" and vid not in runner._parked


def test_restart_awaiting_roi_then_confirm(client, fake_stages, manual_roi):
    vid = upload(client)
    wait_status(client, vid, "awaiting_roi")
    runner._parked.clear()                 # 模拟进程重启：内存里的 _parked 丢失
    runner.recover()
    assert vid in runner._parked and detail(client, vid)["status"] == "awaiting_roi"
    put_roi(client, vid, roi=[0.5, 0, 0.5, 1])
    v = wait_status(client, vid, "awaiting_confirm")
    assert stage_map(v)["segments"]["status"] == "done"
    assert fake_stages.calls.count("asr") == 1


def test_restart_after_confirm_record_continues(client, fake_stages, manual_roi):
    vid = upload(client)
    wait_status(client, vid, "awaiting_roi")
    # 重启前已写入确认记录，但抽帧还没来得及开始（或随进程中断）
    roi_store.save_confirmation(vid, [0, 0, 0.5, 1])
    runner._parked.clear()
    runner.recover()
    v = wait_status(client, vid, "awaiting_confirm")
    assert fake_stages.calls.count("keyframes") == 1 and stage_map(v)["segments"]["status"] == "done"
    assert vid not in runner._parked


def test_delete_video_awaiting_roi(client, fake_stages, manual_roi):
    vid = upload(client)
    wait_status(client, vid, "awaiting_roi")
    put_roi(client, vid, roi=[0.5, 0, 0.5, 1], confirm=False)
    assert client.delete(f"/api/videos/{vid}").json() == {"ok": True}
    assert vid not in runner._parked
    assert client.get(f"/api/videos/{vid}").status_code == 404
    assert db.get_setting(roi_store.video_key(vid)) is None
    assert db.get_setting(roi_store.confirmed_key(vid)) is None
    assert not artifacts.video_dir(vid).exists()
    assert runner.wait_idle(2)


# ---------- 处理完成后修改区域、旧视频、接口参数 ----------

def test_modify_roi_after_done_reruns_only_keyframes_downstream(client, fake_stages):
    vid = upload(client)                    # 自动确认（整个画面）
    wait_status(client, vid, "awaiting_confirm")
    client.post(f"/api/videos/{vid}/confirm", json={})
    wait_status(client, vid, "done")
    fake_stages.calls.clear()

    assert put_roi(client, vid, roi=[0.5, 0, 0.5, 1]).status_code == 200
    v = wait_status(client, vid, "awaiting_confirm")      # 下游含付费阶段，要重新确认发送
    assert fake_stages.calls == ["keyframes", "segments"]
    client.post(f"/api/videos/{vid}/confirm", json={})
    wait_status(client, vid, "done")
    assert fake_stages.calls == ["keyframes", "segments", "read_slides", "evidence", "outline"]
    assert "asr" not in fake_stages.calls and "cues" not in fake_stages.calls
    assert db.get_setting(roi_store.confirmed_key(vid))["roi"] == [0.5, 0, 0.5, 1]


def test_confirm_while_keyframes_running_is_409(client, fake_stages):
    in_kf, release = threading.Event(), threading.Event()
    fake_stages.hooks["keyframes"] = lambda ctx: (in_kf.set(), release.wait(10))
    vid = upload(client)
    assert in_kf.wait(5)
    before = db.get_setting(roi_store.confirmed_key(vid))
    r = put_roi(client, vid, roi=[0.5, 0, 0.5, 1])
    assert r.status_code == 409
    assert db.get_setting(roi_store.confirmed_key(vid)) == before   # 不改确认记录
    release.set()
    wait_status(client, vid, "awaiting_confirm")


def test_legacy_video_counts_as_confirmed(client, fake_stages, manual_roi):
    vid = "0123456789ab"
    db.execute("INSERT INTO videos (id, title, filename, created_at, status) VALUES (?,?,?,?,?)",
               (vid, "old", "old.mp4", db.now_iso(), "done"))
    runner.init_stages(vid)
    db.set_setting(roi_store.video_key(vid), [0.25, 0, 0.75, 1])
    v = detail(client, vid)
    assert v["roi_confirmed"] is False                    # 还没有抽帧结果
    artifacts.write_artifact(vid, "keyframes.json", [], {})
    v = detail(client, vid)
    assert v["roi_confirmed"] is True and v["roi"] == [0.25, 0, 0.75, 1]
    assert keyframes.get_roi(vid) == [0.25, 0, 0.75, 1]   # 沿用原来的区域


def test_confirm_false_only_saves_suggestion(client, fake_stages, manual_roi):
    vid = upload(client)
    wait_status(client, vid, "awaiting_roi")
    r = put_roi(client, vid, roi=[0.1, 0.1, 0.8, 0.8], confirm=False, as_default=True)
    assert r.status_code == 200
    v = r.json()
    assert v["roi_confirmed"] is False and v["roi_suggestion"] == [0.1, 0.1, 0.8, 0.8]
    assert db.get_setting(roi_store.DEFAULT_KEY) == [0.1, 0.1, 0.8, 0.8]
    assert runner.wait_idle(2) and "keyframes" not in fake_stages.calls
    assert detail(client, vid)["status"] == "awaiting_roi"
    assert put_roi(client, vid, roi=[0.9, 0, 0.5, 1]).status_code == 400


def test_new_video_suggestion_uses_global_default(client, fake_stages, manual_roi):
    db.set_setting(roi_store.DEFAULT_KEY, [0, 0, 0.5, 1])
    vid = upload(client)
    v = wait_status(client, vid, "awaiting_roi")
    assert v["roi_confirmed"] is False and v["roi_suggestion"] == [0, 0, 0.5, 1]
    # 不传 roi 直接确认：确认的是此刻的默认值
    put_roi(client, vid)
    v = wait_status(client, vid, "awaiting_confirm")
    assert v["roi"] == [0, 0, 0.5, 1]


def test_get_roi_prefers_confirmed_record(tmp_settings):
    db.set_setting(roi_store.DEFAULT_KEY, [0, 0, 0.5, 1])
    db.set_setting(roi_store.video_key("v1"), [0.5, 0, 0.5, 1])
    roi_store.save_confirmation("v1", [0.1, 0.1, 0.5, 0.5])
    assert keyframes.get_roi("v1") == [0.1, 0.1, 0.5, 0.5]
    assert keyframes.get_roi_suggestion("v1") == [0.5, 0, 0.5, 1]
    roi_store.save_confirmation("v1", None)               # 整个画面：显式保存 [0,0,1,1]，get_roi 返回 None
    assert db.get_setting(roi_store.confirmed_key("v1"))["roi"] == [0, 0, 1, 1]
    assert keyframes.get_roi("v1") is None
