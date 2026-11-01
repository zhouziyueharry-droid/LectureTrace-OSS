"""asr 阶段的增量写入与断点续跑：用假模型，不加载 faster-whisper。"""
import dataclasses
import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from app import artifacts
from app.config import AsrParams, Settings
from app.pipeline import asr
from app.pipeline.base import StageContext

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="需要 ffmpeg")


def seg(start, end, text):
    words = [SimpleNamespace(start=start, end=end, word=" " + text)]
    return SimpleNamespace(start=start, end=end, text=" " + text, words=words)


class FakeModel:
    def __init__(self, segments, fail_after=None):
        self.segments, self.fail_after, self.calls = segments, fail_after, []

    def transcribe(self, path, **kw):
        self.calls.append((path, kw))

        def gen():
            for i, s in enumerate(self.segments):
                if self.fail_after is not None and i == self.fail_after:
                    raise KeyboardInterrupt  # 模拟进程被中断
                yield s
        return gen(), SimpleNamespace(language="en")


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    # 默认用顺序识别测试断点续跑等逻辑；批量识别单独测试
    s = Settings(data_dir=tmp_path, asr=AsrParams(batch_size=0, enabled=True))
    monkeypatch.setattr(artifacts, "settings", s)
    c = StageContext(video_id="v1", stage="asr", settings=s)
    c.dir.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=duration=10",
                    "-ar", "16000", "-ac", "1", str(c.dir / "audio.wav")], check=True)
    artifacts.write_artifact("v1", "audio.json", {"duration_s": 10.0}, {})
    return c


def test_full_run_writes_transcript(ctx, monkeypatch):
    model = FakeModel([seg(0.5, 3.0, "Hello."), seg(4.0, 9.0, "World.")])
    monkeypatch.setattr(asr, "get_model", lambda p, notify=None: (model, "cuda"))
    progress = []
    ctx.on_progress = lambda f, m: progress.append((f, m))
    asr.run(ctx)

    data = ctx.read("transcript.json")
    assert data["language"] == "en"
    assert [s["text"] for s in data["segments"]] == ["Hello.", "World."]
    assert data["segments"][1]["words"] == [{"start": 4.0, "end": 9.0, "word": " World."}]
    assert not (ctx.dir / asr.PARTIAL).exists()
    kw = model.calls[0][1]
    assert kw["vad_filter"] and kw["word_timestamps"] and kw["language"] is None
    assert kw["condition_on_previous_text"] is False
    assert any(abs(f - 0.9) < 1e-6 for f, _ in progress)
    # 产物 params 记录的是配置值
    assert artifacts.read_artifact_meta("v1", "transcript.json")["params"]["model"] == "large-v3"


def test_resume_from_partial(ctx, monkeypatch):
    first = FakeModel([seg(0.5, 3.0, "Hello."), seg(4.0, 5.0, "Part"), seg(6.0, 7.0, "never")], fail_after=2)
    monkeypatch.setattr(asr, "get_model", lambda p, notify=None: (first, "cuda"))
    with pytest.raises(KeyboardInterrupt):
        asr.run(ctx)
    lines = (ctx.dir / asr.PARTIAL).read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["meta"]["language"] == "en"
    assert len(lines) == 3
    # 模拟写了半行
    with (ctx.dir / asr.PARTIAL).open("a", encoding="utf-8") as f:
        f.write('{"start": 5.5, "end"')

    # 续跑：模型看到的是从 5.0 秒截取的音频，时间要加回偏移
    second = FakeModel([seg(0.5, 4.0, "two.")])
    monkeypatch.setattr(asr, "get_model", lambda p, notify=None: (second, "cuda"))
    asr.run(ctx)
    path, kw = second.calls[0]
    assert path.endswith(asr.RESUME_WAV) and kw["language"] == "en"
    segs = ctx.read("transcript.json")["segments"]
    assert [(s["start"], s["end"], s["text"]) for s in segs] == [
        (0.5, 3.0, "Hello."), (4.0, 5.0, "Part"), (5.5, 9.0, "two.")]
    assert segs[2]["words"][0]["start"] == 5.5
    assert not (ctx.dir / asr.RESUME_WAV).exists()


def test_gpu_error_mid_run_falls_back_to_cpu(ctx, monkeypatch):
    class Boom(FakeModel):
        def transcribe(self, path, **kw):
            gen, info = super().transcribe(path, **kw)

            def g():
                yield from gen
                raise RuntimeError("CUDA failed with error out of memory")
            return g(), info

    gpu = Boom([seg(0.0, 2.0, "A.")])
    cpu = FakeModel([seg(0.0, 3.0, "B.")])
    used = []

    def fake_get(p, notify=None):
        used.append(p.device)
        return (gpu, "cuda") if p.device == "cuda" else (cpu, "cpu")

    monkeypatch.setattr(asr, "get_model", fake_get)
    msgs = []
    ctx.on_progress = lambda f, m: msgs.append(m)
    asr.run(ctx)
    assert used == ["cuda", "cpu"]
    assert any("改用 CPU" in m for m in msgs)
    segs = ctx.read("transcript.json")["segments"]
    assert [(s["start"], s["end"]) for s in segs] == [(0.0, 2.0), (2.0, 5.0)]
    # 回退到 CPU 不改变产物参数（只记录模型名），不会被判为需要重跑
    assert artifacts.read_artifact_meta("v1", "transcript.json")["params"] == {"model": ctx.settings.asr.model}


def test_partial_with_other_params_discarded(ctx, monkeypatch):
    (ctx.dir / asr.PARTIAL).write_text(
        json.dumps({"meta": {"language": "de", "params": {"model": "tiny"}}}) + "\n"
        + json.dumps({"start": 0, "end": 8, "text": "alt", "words": []}) + "\n", encoding="utf-8")
    model = FakeModel([seg(1.0, 2.0, "New.")])
    monkeypatch.setattr(asr, "get_model", lambda p, notify=None: (model, "cuda"))
    asr.run(ctx)
    assert [s["text"] for s in ctx.read("transcript.json")["segments"]] == ["New."]
    assert model.calls[0][1]["language"] is None


def test_batched_mode_passes_batch_size(ctx, monkeypatch):
    ctx.settings = dataclasses.replace(ctx.settings, asr=AsrParams(batch_size=16, enabled=True))
    model = FakeModel([seg(0.5, 3.0, "Hello.")])
    monkeypatch.setattr(asr, "get_model", lambda p, notify=None: (model, "cuda"))
    seen = []

    def fake_transcribe(m, src, batch_size, **opts):
        seen.append(batch_size)
        return m.transcribe(src, **opts)

    monkeypatch.setattr(asr, "transcribe", fake_transcribe)
    asr.run(ctx)
    assert seen == [16]
    assert [s["text"] for s in ctx.read("transcript.json")["segments"]] == ["Hello."]


def test_batched_error_falls_back_to_sequential_on_gpu(ctx, monkeypatch):
    # 批量识别中途出错（如显存不足）：保留已写入的段，在 GPU 上改用顺序识别续跑，而不是直接退到 CPU
    ctx.settings = dataclasses.replace(ctx.settings, asr=AsrParams(batch_size=16, enabled=True))
    model = FakeModel([seg(0.0, 3.0, "B.")])
    devices, modes, msgs = [], [], []

    def fake_get(p, notify=None):
        devices.append(p.device)
        return model, "cuda"

    def fake_transcribe(m, src, batch_size, **opts):
        modes.append(batch_size)
        if batch_size:
            def boom():
                yield seg(0.0, 2.0, "A.")
                raise RuntimeError("CUDA out of memory")
            return boom(), SimpleNamespace(language="en")
        return m.transcribe(src, **opts)

    monkeypatch.setattr(asr, "get_model", fake_get)
    monkeypatch.setattr(asr, "transcribe", fake_transcribe)
    ctx.on_progress = lambda f, m: msgs.append(m)
    asr.run(ctx)
    assert modes == [16, 0] and devices == ["cuda", "cuda"]
    assert any("改用顺序识别" in m for m in msgs)
    segs = ctx.read("transcript.json")["segments"]
    assert [(s["start"], s["end"]) for s in segs] == [(0.0, 2.0), (2.0, 5.0)]
