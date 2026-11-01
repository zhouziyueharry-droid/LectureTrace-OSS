"""第一天事项：验证本机显卡上 faster-whisper/CTranslate2 能否用 CUDA float16。

用法：先设置 ASR_ENABLED=true，再 cd backend && uv run --extra cuda python scripts/check_gpu.py [--model tiny]
流程：ffmpeg 生成几秒测试音频（正弦波 + 静音），用指定模型转写，报告成功与否及报错。
正弦波不会识别出文字，这里只验证模型能在 GPU 上加载并完成推理。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.pipeline.asr import add_cuda_dll_dirs, require_enabled  # noqa: E402
from app.pipeline.base import StageError  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="tiny")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--compute", default="float16")
    ap.add_argument("--audio", help="可选：用已有音频文件代替合成音频")
    args = ap.parse_args()

    try:
        require_enabled(settings.asr)
    except StageError as e:
        print(str(e))
        return 1
    try:
        import ctranslate2
        from faster_whisper import WhisperModel
    except ImportError:
        print("缺少语音识别依赖，请在 backend 运行 uv sync --extra cuda（CPU 用 --extra asr）")
        return 1

    added = add_cuda_dll_dirs()
    print(f"已加入 DLL 搜索路径：{added or '无（未安装 nvidia-* wheel）'}")

    print(f"ctranslate2 {ctranslate2.__version__}，CUDA 设备数 {ctranslate2.get_cuda_device_count()}")
    if args.device == "cuda":
        print(f"CUDA 支持的 compute_type：{sorted(ctranslate2.get_supported_compute_types('cuda'))}")

    with tempfile.TemporaryDirectory() as tmp:
        audio = args.audio
        if not audio:
            audio = str(Path(tmp) / "test.wav")
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                            "-i", "sine=frequency=440:duration=5", "-ar", "16000", "-ac", "1", audio],
                           check=True)

        try:
            t0 = time.time()
            model = WhisperModel(args.model, device=args.device, compute_type=args.compute)
            t1 = time.time()
            segments, info = model.transcribe(audio, word_timestamps=True, vad_filter=False,
                                              condition_on_previous_text=False)
            segs = list(segments)  # 生成器，真正推理发生在这里
            t2 = time.time()
        except Exception as e:  # noqa: BLE001
            print(f"\n失败：{type(e).__name__}: {e}")
            traceback.print_exc()
            return 1

    print(f"\n成功：model={args.model} device={args.device} compute_type={args.compute}")
    print(f"加载 {t1 - t0:.1f}s，转写 {t2 - t1:.1f}s，语言 {info.language}，segments {len(segs)}")
    for s in segs[:5]:
        print(f"  [{s.start:.2f}-{s.end:.2f}] {s.text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
