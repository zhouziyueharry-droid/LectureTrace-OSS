"""Local, lossless browser playback copies; source media is never replaced."""
from __future__ import annotations

import json
import os
import struct
import subprocess
import threading
from pathlib import Path

from .pipeline.base import StageError

# ponytail: one process serializes remux jobs; use per-video locks if parallel preparation is needed.
_prepare_lock = threading.Lock()


def run_tool(args: list[str], what: str) -> subprocess.CompletedProcess:
    """运行 ffmpeg/ffprobe，统一把"找不到程序"转成 StageError。"""
    try:
        return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise StageError(f"未找到 {args[0]}，请安装 ffmpeg 并加入 PATH（{what}）") from None


def probe(path: Path) -> dict:
    """返回 {"duration_s": float | None, "has_audio": bool, "has_video": bool}。"""
    p = run_tool(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,duration",
                  "-of", "json", str(path)], "读取视频信息")
    if p.returncode != 0:
        raise StageError(f"无法读取视频文件，可能已损坏或不是有效的视频：{p.stderr.strip()[-300:]}")
    info = json.loads(p.stdout or "{}")
    streams = info.get("streams", [])
    kinds = {s.get("codec_type") for s in streams}
    duration = info.get("format", {}).get("duration")
    if duration in (None, "N/A"):  # 个别文件 format 无时长，退回取流时长
        durs = [float(s["duration"]) for s in streams if s.get("duration") not in (None, "N/A")]
        duration = max(durs) if durs else None
    return {"duration_s": float(duration) if duration is not None else None,
            "has_audio": "audio" in kinds, "has_video": "video" in kinds}


def needs_remux(path: Path, max_boxes: int = 20000) -> bool:
    """扫描 MP4 顶层 box：有 moof（分片）或 moov 在 mdat 之后时返回 True。只读 box 头，很快。"""
    seen_mdat = False
    with path.open("rb") as f:
        size_total = path.stat().st_size
        pos = 0
        for _ in range(max_boxes):
            if pos + 8 > size_total:
                return False
            f.seek(pos)
            size, kind = struct.unpack(">I4s", f.read(8))
            if size == 1:  # 64 位长度
                size = struct.unpack(">Q", f.read(8))[0]
            elif size == 0:  # 延伸到文件末尾
                size = size_total - pos
            if kind == b"moof":
                return True
            if kind == b"moov" and seen_mdat:
                return True  # 索引在数据之后；索引在前时继续扫描，分片 MP4 的 moof 在 moov 之后
            if kind == b"mdat":
                seen_mdat = True
            if size < 8:
                return False  # 结构异常，不处理
            pos += size
    return False


def playback_file(src: Path) -> Path:
    """Return the original faststart MP4 or an atomically published, source-versioned copy."""
    try:
        if not needs_remux(src):
            return src
        signature = src.stat()
        target = src.parent / ".playback" / f"{signature.st_size}-{signature.st_mtime_ns}.mp4"
        if target.is_file() and target.stat().st_size:
            return target
        with _prepare_lock:
            if target.is_file() and target.stat().st_size:
                return target
            target.parent.mkdir(exist_ok=True)
            tmp = target.with_suffix(".tmp.mp4")
            try:
                p = run_tool(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-map", "0",
                              "-c", "copy", "-movflags", "+faststart", str(tmp)], "准备播放副本")
                if p.returncode != 0 or not tmp.is_file() or not tmp.stat().st_size:
                    raise StageError(f"无法准备播放副本，原视频已保留：{p.stderr.strip()[-300:]}")
                if needs_remux(tmp) or not probe(tmp)["has_video"]:
                    raise StageError("播放副本校验失败，原视频已保留")
                current = src.stat()
                if (current.st_size, current.st_mtime_ns) != (signature.st_size, signature.st_mtime_ns):
                    raise StageError("准备播放期间原视频发生变化，请重试")
                os.replace(tmp, target)
            finally:
                tmp.unlink(missing_ok=True)
        return target
    except (OSError, struct.error) as e:
        raise StageError(f"无法准备播放副本，原视频已保留：{e}") from e
