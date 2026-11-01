"""共用测试夹具。各 agent 需要时在文件末尾追加，不要覆盖已有内容。"""
from __future__ import annotations

import dataclasses
import sys
import threading
import types

import pytest


# ---------- 配置与数据目录（Agent C） ----------

@pytest.fixture
def set_settings(monkeypatch):
    """返回 set(new_settings)：把所有 app.* 模块中对当前 settings 的引用替换为 new_settings。"""
    import app.artifacts  # noqa: F401  确保共享模块已导入
    import app.db  # noqa: F401
    import app.pipeline.base  # noqa: F401
    from app import config

    def _set(new):
        cur = config.settings
        for name, mod in list(sys.modules.items()):
            if mod is None or not (name == "app" or name.startswith("app.")):
                continue
            for attr in ("settings", "default_settings"):
                if getattr(mod, attr, None) is cur:
                    monkeypatch.setattr(mod, attr, new)
        return new

    return _set


@pytest.fixture(autouse=True)
def tmp_settings(tmp_path, set_settings):
    """data_dir 指向 tmp_path/data 并建好数据库。

    测试默认跑完整的 9 个阶段、用 LLM 翻译（不受 .env 与"暂未启用"默认值影响）；需要时各测试自行覆盖。
    """
    from app import config, db
    s = set_settings(dataclasses.replace(
        config.settings, data_dir=tmp_path / "data", disabled_stages=(), llm_provider="anthropic",
        translate=config.TranslateParams(provider="llm"), deepl_api_key="", openai_api_key="",
        anthropic_api_key="", deepseek_api_key="", google_translate_api_key=""))
    db.init_db()
    db.set_setting("llm_settings", {"outline_enabled": True})
    return s


# ---------- 假阶段模块（Agent C 的 runner / api 测试用） ----------

class FakeStages:
    """用 sys.modules 注入 app.pipeline.<module> 的假实现，记录调用顺序，可设定失败。"""

    def __init__(self):
        self.calls: list[str] = []
        self.fail: dict[str, Exception] = {}          # 阶段名 -> 下一次运行抛出的异常（只抛一次）
        self.hooks: dict[str, callable] = {}           # 阶段名 -> 额外动作 f(ctx)
        self.lock = threading.Lock()

    def make_run(self, stage: str):
        def run(ctx):
            with self.lock:
                self.calls.append(stage)
                exc = self.fail.pop(stage, None)
            if stage in self.hooks:
                self.hooks[stage](ctx)
            if exc is not None:
                raise exc
            _fake_outputs(ctx)
        return run


def _fake_outputs(ctx):
    d = ctx.dir
    s = ctx.stage
    if s == "extract_audio":
        (d / "audio.wav").write_bytes(b"RIFF")
        ctx.write("audio.json", {"duration_s": 12.5})
    elif s == "asr":
        ctx.write("transcript.json", {"language": "en", "segments": []})
    elif s == "cues":
        ctx.write("cues.json", [{"id": "C0001", "start": 0.0, "end": 1.0, "text": "Hi.", "zh": None}])
    elif s == "keyframes":
        (d / "keyframes").mkdir(exist_ok=True)
        (d / "keyframes" / "S001.jpg").write_bytes(b"\xff\xd8JPEG")
        ctx.write("keyframes.json", [{"id": "S001", "image": "keyframes/S001.jpg", "appearances": [[0, 1]]}])
    elif s == "segments":  # 格式见交接 0001 §4.3
        ctx.write("segments.json", {
            "sections": [{"id": "S001.1", "page": "S001", "display_start": 0.0, "display_end": 1.0,
                          "speech_start": 0.0, "speech_end": 1.0, "image": "keyframes/S001.jpg",
                          "t_ids": ["T0001"], "local_tags": []}],
            "speech": [{"id": "T0001", "section": "S001.1", "start": 0.0, "end": 1.0,
                        "cue_first": "C0001", "cue_last": "C0001", "text": "Hi."}]})
    elif s == "translate":
        from app import artifacts
        artifacts.write_artifact(ctx.video_id, "cues.json",
                                 [{"id": "C0001", "start": 0.0, "end": 1.0, "text": "Hi.", "zh": "嗨。"}],
                                 ctx.settings.stage_params("cues"))
        ctx.write("translate.json", {"cue_count": 1})
        (d / "subs.source.vtt").write_text("WEBVTT\n\n00:00.000 --> 00:01.000\nHi.\n", encoding="utf-8")
        (d / "subs.bilingual.vtt").write_text("WEBVTT\n\n00:00.000 --> 00:01.000\nHi.\n嗨。\n",
                                              encoding="utf-8")
    elif s == "read_slides":
        ctx.write("slides.json", [{"id": "S001", "title": "T", "bullets": [], "formulas": [], "figure": "",
                                   "emphasis": "", "appearances": [[0, 1]]}])
    elif s == "evidence":
        ctx.write("evidence.json", [{"id": "S001", "kind": "slide", "start": 0, "end": 1, "jump": 0,
                                     "text": "T"}])
    elif s == "outline":
        ctx.write("outline.json", {"chapters": [{"title": "T", "summary": "", "evidence": ["S001"],
                                                 "start": 0.0, "children": []}], "warnings": []})


@pytest.fixture
def fake_stages(monkeypatch, roi_auto_confirm):
    from app.pipeline import runner
    fs = FakeStages()
    for stage, module in runner.STAGE_MODULES.items():
        mod = types.ModuleType(f"app.pipeline.{module}")
        mod.run = fs.make_run(stage)
        monkeypatch.setitem(sys.modules, f"app.pipeline.{module}", mod)
    yield fs
    runner.wait_idle(10)
    with runner._lock:   # 内存状态不跨测试残留（test_runner 里视频 ID 是固定的）
        runner._parked.clear()
    runner._hooks.clear()


# ---------- 幻灯片区域确认（0001 §2.5，I 组） ----------

class RoiAutoConfirm:
    enabled = True


@pytest.fixture
def roi_auto_confirm(monkeypatch):
    """新流程要先确认幻灯片区域才会抽帧。为了让"导入即自动跑完"的旧测试不变，
    建立阶段行（上传、make_video、入队时的 runner.init_stages）时自动按整个画面确认。

    测 ROI 确认流程本身的测试把 `.enabled` 设为 False（或直接用 manual_roi 夹具）。
    """
    from app.pipeline import roi as roi_store
    from app.pipeline import runner

    state = RoiAutoConfirm()
    orig = runner.init_stages

    def init_stages(video_id):
        orig(video_id)
        if state.enabled and not roi_store.is_confirmed(video_id):
            roi_store.save_confirmation(video_id, None)

    monkeypatch.setattr(runner, "init_stages", init_stages)
    return state


@pytest.fixture
def manual_roi(roi_auto_confirm):
    """关闭自动确认：新视频停在"等待确认幻灯片区域"。"""
    roi_auto_confirm.enabled = False
    return roi_auto_confirm


# ---------- 外部 API 阶段（Agent B） ----------

@pytest.fixture
def data_dir(tmp_settings, set_settings):
    """在 tmp_settings 基础上清空 API Key（防止误发真实请求），返回数据目录。"""
    s = set_settings(dataclasses.replace(tmp_settings, anthropic_api_key="", deepl_api_key=""))
    return s.data_dir
