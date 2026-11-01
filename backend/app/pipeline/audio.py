"""阶段 extract_audio：source.mp4 → audio.wav（16 kHz 单声道）+ audio.json（时长）。

开始前先检查视频能否在浏览器里流畅播放：分片 MP4（fMP4，课程平台和录屏软件常见）或索引在文件末尾的 MP4，
浏览器要发成千上万次 Range 请求才能拿到时长、无法拖动。遇到时用 ffmpeg 生成独立的无损播放副本（-c copy + faststart），不替换原件。
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .base import StageContext, StageError

from ..playback import needs_remux, playback_file, probe, run_tool


def cut_wav(src: Path, dst: Path, start_s: float) -> None:
    """从 start_s 截取到结尾，输出 16 kHz 单声道 wav（asr 续跑用）。"""
    p = run_tool(["ffmpeg", "-y", "-v", "error", "-ss", f"{start_s:.3f}", "-i", str(src),
                  "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-f", "wav", str(dst)], "截取音频")
    if p.returncode != 0:
        raise StageError(f"截取音频失败：{p.stderr.strip()[-300:]}")


def make_playable(ctx: StageContext) -> None:
    """Prewarm the same lossless copy used by HTTP playback; failures must remain visible."""
    if needs_remux(ctx.source):
        ctx.progress(0.0, "正在无损准备播放副本，原视频保持不变")
    playback_file(ctx.source)


def run(ctx: StageContext) -> None:
    src = ctx.source
    if not src.exists():
        raise StageError("找不到视频文件 source.mp4")
    make_playable(ctx)

    info = probe(src)
    if not info["has_audio"]:
        raise StageError("视频没有音轨，无法生成字幕")
    duration = info["duration_s"]

    out = ctx.dir / "audio.wav"
    tmp = ctx.dir / "audio.wav.tmp"
    args = ["ffmpeg", "-y", "-v", "error", "-nostats", "-progress", "pipe:1", "-i", str(src),
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-f", "wav", str(tmp)]
    ctx.progress(0.0, "正在提取音频")
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise StageError("未找到 ffmpeg，请安装 ffmpeg 并加入 PATH") from None
    # -progress 每隔约 0.5 秒输出 key=value 行，据此报告进度
    assert proc.stdout is not None
    for line in proc.stdout:
        key, _, val = line.strip().partition("=")
        if key == "out_time_us" and duration and val.isdigit():
            ctx.progress(int(val) / 1e6 / duration, "正在提取音频")
    err = proc.stderr.read() if proc.stderr else ""
    if proc.wait() != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        raise StageError(f"提取音频失败：{err.strip()[-300:]}")
    os.replace(tmp, out)

    if duration is None:  # 视频容器没给时长，用抽出的音频再取一次
        duration = probe(out)["duration_s"]
    if not duration:
        raise StageError("无法确定视频时长")
    ctx.write("audio.json", {"duration_s": round(duration, 3)})
    ctx.progress(1.0, "音频提取完成")
