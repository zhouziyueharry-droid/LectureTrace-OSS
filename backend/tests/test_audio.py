"""extract_audio 阶段：用 ffmpeg 现场生成几秒测试视频。"""
import shutil
import subprocess
import wave

import pytest

from app import artifacts
from app.config import Settings
from app.pipeline import audio
from app.pipeline.base import StageContext, StageError

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="需要 ffmpeg")


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    s = Settings(data_dir=tmp_path)
    monkeypatch.setattr(artifacts, "settings", s)
    c = StageContext(video_id="v1", stage="extract_audio", settings=s)
    c.dir.mkdir(parents=True)
    return c


def make_video(path, seconds=3, with_audio=True):
    args = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=10:duration={seconds}"]
    if with_audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=44100:duration={seconds}"]
    args += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-shortest", str(path)]
    subprocess.run(args, check=True)


def test_extract_audio(ctx):
    make_video(ctx.source, seconds=3)
    progress = []
    ctx.on_progress = lambda f, m: progress.append(f)
    audio.run(ctx)

    with wave.open(str(ctx.dir / "audio.wav")) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1
        assert abs(w.getnframes() / 16000 - 3) < 0.2
    meta = artifacts.read_json(ctx.dir / "audio.json")
    assert meta["schema_version"] == 1
    assert abs(meta["data"]["duration_s"] - 3) < 0.2
    assert progress[-1] == 1.0
    assert not (ctx.dir / "audio.wav.tmp").exists()


def test_no_audio_track(ctx):
    make_video(ctx.source, seconds=2, with_audio=False)
    with pytest.raises(StageError, match="没有音轨"):
        audio.run(ctx)


def test_missing_or_broken_source(ctx):
    with pytest.raises(StageError, match="找不到"):
        audio.run(ctx)
    ctx.source.write_bytes(b"not a video")
    with pytest.raises(StageError, match="无法读取"):
        audio.run(ctx)


def test_cut_wav(ctx, tmp_path):
    make_video(ctx.source, seconds=3)
    audio.run(ctx)
    out = tmp_path / "cut.wav"
    audio.cut_wav(ctx.dir / "audio.wav", out, 1.0)
    with wave.open(str(out)) as w:
        assert abs(w.getnframes() / 16000 - 2) < 0.1


def _make_mp4(path, movflags):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=4:size=160x120:rate=10",
                    "-f", "lavfi", "-i", "sine=duration=4", "-c:v", "libx264", "-g", "10", "-c:a", "aac",
                    "-movflags", movflags, "-shortest", str(path)], check=True)


def test_needs_remux_detects_fragmented_and_moov_at_end(tmp_path):
    from app.pipeline.audio import needs_remux

    frag, fast, tail = tmp_path / "frag.mp4", tmp_path / "fast.mp4", tmp_path / "tail.mp4"
    _make_mp4(frag, "frag_keyframe+empty_moov")
    _make_mp4(fast, "+faststart")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(fast), "-c", "copy", str(tail)], check=True)
    assert needs_remux(frag) is True
    assert needs_remux(fast) is False
    assert needs_remux(tail) is True   # 默认封装：moov 在 mdat 之后


def test_run_remuxes_fragmented_source(tmp_settings):
    from app.pipeline import audio
    from app.pipeline.base import StageContext

    ctx = StageContext(video_id="vfrag", stage="extract_audio", settings=tmp_settings)
    ctx.dir.mkdir(parents=True)
    _make_mp4(ctx.source, "frag_keyframe+empty_moov")
    before = audio.probe(ctx.source)["duration_s"]
    msgs = []
    ctx.on_progress = lambda f, m: msgs.append(m)
    audio.run(ctx)
    assert audio.needs_remux(ctx.source) is True  # 原件保留，播放使用独立副本
    assert audio.needs_remux(audio.playback_file(ctx.source)) is False
    assert abs(audio.probe(ctx.source)["duration_s"] - before) < 0.2
    assert any("播放副本" in m for m in msgs)
    assert not (ctx.dir / "source.remux.mp4").exists()
    assert (ctx.dir / "audio.wav").exists()
