"""runner 与 events 的测试：阶段模块全部替换为假实现（见 conftest.fake_stages）。"""
from __future__ import annotations

import dataclasses
import time

import pytest

from app import artifacts, db, events
from app.config import CueParams, KeyframeParams
from app.pipeline import runner
from app.pipeline.base import API_STAGES, LOCAL_STAGES, STAGES, StageError


@pytest.fixture
def env(tmp_settings, fake_stages):
    runner.start_worker()
    return fake_stages


def make_video(video_id: str = "abc123def456") -> str:
    db.execute("INSERT INTO videos (id, title, filename, created_at, status) VALUES (?,?,?,?,?)",
               (video_id, "t", "t.mp4", db.now_iso(), "processing"))
    artifacts.video_dir(video_id).mkdir(parents=True, exist_ok=True)
    (artifacts.video_dir(video_id) / "source.mp4").write_bytes(b"0" * 10)
    runner.init_stages(video_id)
    return video_id


def wait_status(video_id: str, *statuses: str, timeout: float = 10) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = db.fetch_one("SELECT * FROM videos WHERE id = ?", (video_id,))
        if v and v["status"] in statuses and runner.wait_idle(0.05):
            return v
        time.sleep(0.02)
    raise AssertionError(f"等待状态 {statuses} 超时，当前 {v and v['status']}")


def stages(video_id: str) -> dict[str, dict]:
    return {r["name"]: r for r in db.fetch_all("SELECT * FROM stages WHERE video_id = ?", (video_id,))}


def test_runs_pauses_at_confirm_then_finishes(env):
    vid = make_video()
    with events.subscribe_sync(vid) as q:
        runner.enqueue(vid)
        v = wait_status(vid, "awaiting_confirm")
        got = []
        while not q.empty():
            got.append(q.get())

    assert v["duration_s"] == 12.5
    st = stages(vid)
    for name in LOCAL_STAGES:
        assert st[name]["status"] == "done" and st[name]["attempt"] == 1
        assert st[name]["started_at"] and st[name]["finished_at"]
    assert st["await_confirm"]["status"] == "running"
    assert all(st[n]["status"] == "pending" for n in API_STAGES)
    # 抽帧在独立线程，与语音识别谁先记录不确定；只要求抽帧在切分讲解片段之前完成
    assert sorted(env.calls) == sorted(LOCAL_STAGES)
    assert env.calls.index("keyframes") < env.calls.index("segments")
    # 事件：每个本机阶段 running → done，最后视频进入 awaiting_confirm
    stage_evs = [(e["stage"], e["status"]) for e in got if e["type"] == "stage"]
    assert ("extract_audio", "running") in stage_evs and ("keyframes", "done") in stage_evs
    assert got[-1] == {"type": "video", "status": "awaiting_confirm"}

    runner.confirm(vid, remember=False)
    wait_status(vid, "done")
    st = stages(vid)
    assert all(st[n]["status"] == "done" for n in STAGES)
    assert sorted(env.calls) == sorted(n for n in STAGES if n != "await_confirm")
    assert db.get_setting("skip_confirm") is None


def test_confirm_when_not_waiting_raises(env):
    vid = make_video()
    with pytest.raises(ValueError):
        runner.confirm(vid)


def test_failure_then_retry_resumes(env):
    vid = make_video()
    env.fail["read_slides"] = StageError("读幻灯片失败：网络超时")
    runner.enqueue(vid)
    wait_status(vid, "awaiting_confirm")
    runner.confirm(vid)
    wait_status(vid, "failed")

    st = stages(vid)
    assert st["translate"]["status"] == "done"
    assert st["read_slides"]["status"] == "failed"
    assert st["read_slides"]["error"] == "读幻灯片失败：网络超时"
    assert st["evidence"]["status"] == "pending"
    assert (artifacts.video_dir(vid) / "subs.bilingual.vtt").exists()  # 已有结果保留

    env.calls.clear()
    runner.enqueue(vid)
    wait_status(vid, "done")
    assert env.calls == ["read_slides", "evidence", "outline"]  # 从失败阶段继续
    st = stages(vid)
    assert st["read_slides"]["attempt"] == 2 and st["read_slides"]["error"] is None
    assert st["translate"]["attempt"] == 1


def test_internal_error_message(env):
    vid = make_video()
    env.fail["asr"] = KeyError("segments")
    runner.enqueue(vid)
    wait_status(vid, "failed")
    err = stages(vid)["asr"]["error"]
    assert err.startswith("内部错误：KeyError:")


def run_to_done(vid):
    """入队 → 在发送确认处确认 → 跑完。"""
    runner.enqueue(vid)
    wait_status(vid, "awaiting_confirm")
    runner.confirm(vid)
    wait_status(vid, "done")


def test_skip_confirm_only_when_no_gpt_stage(env):
    db.set_setting("skip_confirm", True)
    # 要调用 GPT（读幻灯片、生成大纲）时，即使选了"以后不再询问"也会停下，先看费用预估
    vid = make_video()
    runner.enqueue(vid)
    wait_status(vid, "awaiting_confirm")

    # 没有 GPT 阶段要跑（未开启大纲生成）时直接放行
    db.set_setting("llm_settings", {"outline_enabled": False})
    vid2 = make_video("bbbbbbbbbbbb")
    runner.enqueue(vid2)
    wait_status(vid2, "done")
    st = stages(vid2)
    assert st["await_confirm"]["status"] == "skipped"
    assert st["translate"]["status"] == "done"
    assert all(st[n]["status"] == "skipped" and st[n]["error"] == runner.DISABLED_REASON
               for n in runner.OUTLINE_STAGES)


def test_confirm_remember_sets_skip(env):
    vid = make_video()
    runner.enqueue(vid)
    wait_status(vid, "awaiting_confirm")
    runner.confirm(vid, remember=True)
    wait_status(vid, "done")
    assert db.get_setting("skip_confirm") is True
    db.set_setting("llm_settings", {"outline_enabled": False})  # 只剩 DeepL 翻译时才自动放行
    vid2 = make_video("bbbbbbbbbbbb")
    runner.enqueue(vid2)
    wait_status(vid2, "done")
    assert stages(vid2)["await_confirm"]["status"] == "skipped"


def test_google_never_uses_free_translation_auto_confirm(env):
    db.set_setting('translation_provider', 'google')
    db.set_setting('skip_confirm', True)
    db.set_setting('llm_settings', {'outline_enabled': False})
    vid = make_video()
    runner.enqueue(vid)
    wait_status(vid, 'awaiting_confirm')
    assert 'translate' not in env.calls


def test_progress_is_throttled(env):
    vid = make_video()
    env.hooks["asr"] = lambda ctx: [ctx.progress(i / 5000, "识别中") for i in range(5001)]
    with events.subscribe_sync(vid) as q:
        runner.enqueue(vid)
        wait_status(vid, "awaiting_confirm")
        evs = [q.get() for _ in range(q.qsize())]
    progress = [e for e in evs if e["type"] == "stage" and e["stage"] == "asr" and e["status"] == "running"]
    assert 2 <= len(progress) <= 110
    assert progress[-1]["progress"] == 1.0


def test_recover_after_restart(env):
    vid = make_video()
    for name in ("extract_audio", "keyframes", "asr"):
        db.execute("UPDATE stages SET status='done' WHERE video_id=? AND name=?", (vid, name))
    db.execute("UPDATE stages SET status='running' WHERE video_id=? AND name='cues'", (vid,))
    runner.recover()
    wait_status(vid, "awaiting_confirm")
    assert env.calls == ["cues", "segments"]   # 从中断的阶段继续


def test_recover_keeps_awaiting_confirm(env):
    vid = make_video()
    runner.enqueue(vid)
    wait_status(vid, "awaiting_confirm")
    runner.recover()
    assert stages(vid)["await_confirm"]["status"] == "running"
    runner.confirm(vid)
    wait_status(vid, "done")


def test_stale_stages(env, set_settings):
    vid = make_video()
    run_to_done(vid)
    assert runner.stale_stages(vid) == []

    from app import config
    base = config.settings
    set_settings(dataclasses.replace(base, cues=CueParams(max_chars=60)))
    assert runner.stale_stages(vid) == ["cues", "segments", "translate", "read_slides", "evidence", "outline"]

    set_settings(dataclasses.replace(base, llm_model="other-model"))
    assert runner.stale_stages(vid) == ["translate", "read_slides", "evidence", "outline"]

    # 关键帧参数变化不影响 asr/cues/translate
    set_settings(dataclasses.replace(base, keyframe=KeyframeParams(stable_s=5.0)))
    assert runner.stale_stages(vid) == ["keyframes", "segments", "read_slides", "evidence", "outline"]

    # 未完成的阶段不判断
    set_settings(base)
    db.execute("UPDATE stages SET status='pending' WHERE video_id=? AND name='outline'", (vid,))
    set_settings(dataclasses.replace(base, llm_model="other-model"))
    assert runner.stale_stages(vid) == ["translate", "read_slides", "evidence"]


def test_stale_detects_artifact_schema_change(env):
    vid = make_video()
    run_to_done(vid)
    path = artifacts.video_dir(vid) / "evidence.json"
    obj = artifacts.read_json(path)
    obj["schema_version"] = 0
    artifacts.write_json(path, obj)
    assert runner.stale_stages(vid) == ["evidence", "outline"]


def test_reset_stages(env):
    vid = make_video()
    runner.enqueue(vid)
    wait_status(vid, "awaiting_confirm")
    runner.confirm(vid)
    wait_status(vid, "done")

    # keyframes 的下游含外部 API 阶段 read_slides，所以 await_confirm 也被重置，需要重新确认
    assert runner.reset_stages(vid, ["keyframes"]) == ["keyframes", "segments", "read_slides", "evidence", "outline",
                                                      "await_confirm"]
    env.calls.clear()
    runner.enqueue(vid)
    wait_status(vid, "awaiting_confirm")
    assert env.calls == ["keyframes", "segments"]
    runner.confirm(vid)
    wait_status(vid, "done")
    assert env.calls == ["keyframes", "segments", "read_slides", "evidence", "outline"]

    with pytest.raises(ValueError):
        runner.reset_stages(vid, ["nope"])


def test_downstream():
    assert runner.downstream(["cues"]) == ["cues", "segments", "translate", "read_slides", "evidence", "outline"]
    assert runner.downstream(["asr"])[:2] == ["asr", "cues"]
    assert runner.downstream(["translate"]) == ["translate"]
    assert runner.downstream(["keyframes"]) == ["keyframes", "segments", "read_slides", "evidence", "outline"]
    assert runner.downstream(["segments"]) == ["segments", "read_slides", "evidence", "outline"]
    assert "segments" not in runner.OUTLINE_STAGES and "segments" in LOCAL_STAGES
