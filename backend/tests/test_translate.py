import dataclasses
import json

import pytest

from app import artifacts, config
from app.pipeline import translate
from app.pipeline.base import StageContext, StageError

VID = "vtr"


def make_cues(n):
    return [{"id": f"C{i:04d}", "start": i * 2.0, "end": i * 2.0 + 1.5, "text": f"sentence {i}", "zh": None}
            for i in range(1, n + 1)]


def setup_video(cues):
    artifacts.write_artifact(VID, "cues.json", cues, config.settings.stage_params("cues"))
    return StageContext(video_id=VID, stage="translate")


class FakeLLM:
    """按请求里的 cues 回 {id: 中文}；可指定前几次返回错误条数。"""

    def __init__(self, bad_first=0):
        self.calls = []
        self.bad_first = bad_first

    def __call__(self, *, messages, **kw):
        payload = json.loads(messages[0]["content"])
        self.calls.append(payload)
        out = {c["id"]: f"中文{c['id']}" for c in payload["cues"]}
        if len(self.calls) <= self.bad_first:
            out.pop(next(iter(out)))  # 少一条
        return out


def test_llm_batches_context_and_vtt(data_dir, monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(translate.llm, "complete_json", fake)
    ctx = setup_video(make_cues(85))
    progress = []
    ctx.on_progress = lambda f, m: progress.append(f)

    translate.run(ctx, translator=translate.LLMTranslator())

    # 85 条 → 40 + 40 + 5
    assert [len(c["cues"]) for c in fake.calls] == [40, 40, 5]
    assert fake.calls[0]["context_before"] == []
    assert [c["id"] for c in fake.calls[1]["context_before"]] == ["C0039", "C0040"]
    assert [c["id"] for c in fake.calls[1]["context_after"]] == ["C0081", "C0082"]
    assert fake.calls[2]["context_after"] == []
    assert progress[-1] == 1.0

    cues = ctx.read("cues.json")
    assert all(c["zh"] == f"中文{c['id']}" for c in cues)
    # 完成后 cues.json 的 params 是 translate 的
    assert artifacts.read_artifact_meta(VID, "cues.json")["params"] == ctx.params

    src = (ctx.dir / "subs.source.vtt").read_text(encoding="utf-8")
    bi = (ctx.dir / "subs.bilingual.vtt").read_text(encoding="utf-8")
    assert src.startswith("WEBVTT\n\nC0001\n00:00:02.000 --> 00:00:03.500\nsentence 1\n\n")
    assert "C0001\n00:00:02.000 --> 00:00:03.500\nsentence 1\n中文C0001\n\n" in bi
    assert "中文" not in src


def test_retry_on_count_mismatch(data_dir, monkeypatch):
    fake = FakeLLM(bad_first=1)
    monkeypatch.setattr(translate.llm, "complete_json", fake)
    ctx = setup_video(make_cues(5))
    translate.run(ctx, translator=translate.LLMTranslator())
    assert len(fake.calls) == 2
    assert all(c["zh"] for c in ctx.read("cues.json"))


def test_split_after_repeated_failure(data_dir, monkeypatch):
    # 前 3 次都缺一条 → 拆成两半后成功
    fake = FakeLLM(bad_first=3)
    monkeypatch.setattr(translate.llm, "complete_json", fake)
    ctx = setup_video(make_cues(4))
    translate.run(ctx, translator=translate.LLMTranslator())
    assert [len(c["cues"]) for c in fake.calls[3:]] == [2, 2]
    assert all(c["zh"] for c in ctx.read("cues.json"))


def test_single_cue_failure_raises(data_dir, monkeypatch):
    monkeypatch.setattr(translate.llm, "complete_json", lambda **kw: {})
    ctx = setup_video(make_cues(1))
    with pytest.raises(StageError):
        translate.run(ctx, translator=translate.LLMTranslator())


def test_resume_skips_translated(data_dir, monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(translate.llm, "complete_json", fake)
    cues = make_cues(10)
    for c in cues[:6]:
        c["zh"] = "已有"
    ctx = setup_video(cues)
    translate.run(ctx, translator=translate.LLMTranslator())
    assert [c["id"] for c in fake.calls[0]["cues"]] == ["C0007", "C0008", "C0009", "C0010"]
    assert [c["id"] for c in fake.calls[0]["context_before"]] == ["C0005", "C0006"]
    out = ctx.read("cues.json")
    assert out[0]["zh"] == "已有" and out[9]["zh"] == "中文C0010"


def test_checkpoint_keeps_cue_params(data_dir, monkeypatch):
    """中途失败时，已完成批次已落盘，且 cues.json 仍带 cues 阶段参数。"""
    calls = {"n": 0}

    def flaky(*, messages, **kw):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("网络断了")
        payload = json.loads(messages[0]["content"])
        return {c["id"]: "译文" for c in payload["cues"]}

    monkeypatch.setattr(translate.llm, "complete_json", flaky)
    ctx = setup_video(make_cues(50))
    with pytest.raises(RuntimeError):
        translate.run(ctx, translator=translate.LLMTranslator())
    cues = ctx.read("cues.json")
    assert sum(1 for c in cues if c["zh"]) == 40
    assert artifacts.read_artifact_meta(VID, "cues.json")["params"] == config.settings.stage_params("cues")


def test_vtt_escape_and_newlines():
    vtt = translate.build_vtt([{"id": "C0001", "start": 3661.5, "end": 3662, "text": "a < b\n\nand c",
                                "zh": "甲 & 乙"}], bilingual=True)
    assert "01:01:01.500 --> 01:01:02.000" in vtt
    assert "a &lt; b and c\n甲 &amp; 乙\n" in vtt


def test_unknown_provider(data_dir):
    s = dataclasses.replace(config.settings, translate=config.TranslateParams(provider="foo"))
    with pytest.raises(StageError):
        translate.make_translator(s)
