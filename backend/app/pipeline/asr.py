"""阶段 asr：audio.wav → transcript.json（faster-whisper，分段 + 词级时间戳，ARCHITECTURE §3.1）。

- 每识别出一个 segment 就追加到 transcript.partial.jsonl；中断后从最后一个 segment 的结束时间续跑
  （ffmpeg -ss 截取剩余音频到临时 wav，时间加回偏移）。
- CUDA 初始化或推理失败时自动退回 device="cpu", compute_type="int8"。
- 模型对象在进程内缓存。
"""
from __future__ import annotations

import json
import importlib.util
import os
import sys
import threading
from dataclasses import replace
from pathlib import Path

from ..config import AsrParams
from .audio import cut_wav, probe
from .base import StageContext, StageError

PARTIAL = "transcript.partial.jsonl"
RESUME_WAV = "asr_resume.wav"
CPU_FALLBACK = ("cpu", "int8")

_models: dict[tuple[str, str, str], object] = {}
_lock = threading.Lock()
_dll_dirs_added: list[str] | None = None


def add_cuda_dll_dirs() -> list[str]:
    """Windows 上把 pip 安装的 nvidia-cublas-cu12 / nvidia-cudnn-cu12 等包的 bin 目录加入 DLL 搜索路径。

    CTranslate2 运行时才加载 cublas/cudnn，所以同时加入 os.add_dll_directory 和 PATH。
    未安装这些包时什么也不做。返回加入的目录列表。
    """
    global _dll_dirs_added
    if _dll_dirs_added is not None:
        return _dll_dirs_added
    added: list[str] = []
    if sys.platform == "win32":
        import importlib.util
        spec = importlib.util.find_spec("nvidia")
        roots = list(spec.submodule_search_locations or []) if spec else []
        for root in roots:
            for bin_dir in sorted(Path(root).glob("*/bin")):
                if any(bin_dir.glob("*.dll")):
                    os.add_dll_directory(str(bin_dir))
                    os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
                    added.append(str(bin_dir))
    _dll_dirs_added = added
    return added


def _load(model: str, device: str, compute_type: str):
    """加载并预热模型（预热会触发 cuBLAS/cuDNN 加载，尽早暴露 CUDA 问题）。"""
    import numpy as np
    from faster_whisper import WhisperModel

    key = (model, device, compute_type)
    with _lock:
        if key not in _models:
            m = WhisperModel(model, device=device, compute_type=compute_type)
            segs, _ = m.transcribe(np.zeros(16000, dtype=np.float32), language="en", vad_filter=False)
            list(segs)
            _models[key] = m
        return _models[key]


def transcribe(model, src: str, batch_size: int, **opts):
    """batch_size > 0 时用批量识别（按语音段分批同时推理），否则顺序识别。返回 (segments 迭代器, info)。"""
    if batch_size > 0:
        from faster_whisper import BatchedInferencePipeline

        return BatchedInferencePipeline(model).transcribe(src, batch_size=batch_size, **opts)
    return model.transcribe(src, **opts)


def require_enabled(params: AsrParams) -> None:
    if not params.enabled:
        raise StageError("本地语音识别（ASR）未启用。安装可选依赖（uv sync --extra asr，"
                         "GPU 模式用 --extra cuda），设置 ASR_ENABLED=true 后重启后端并重试。")


def get_model(params: AsrParams, notify=lambda msg: None):
    """按配置取模型；CUDA 失败时退回 CPU int8。返回 (model, device)。"""
    require_enabled(params)
    if importlib.util.find_spec("faster_whisper") is None:
        raise StageError("本地语音识别（ASR）缺少 faster-whisper。请在 backend 目录运行 "
                         "uv sync --extra asr（GPU 模式用 --extra cuda），然后重启后端。")
    add_cuda_dll_dirs()
    if params.device != "cpu":
        try:
            return _load(params.model, params.device, params.compute_type), params.device
        except Exception as e:  # noqa: BLE001  CUDA 相关错误类型不固定
            notify(f"GPU 初始化失败（{_short(e)}），已改用 CPU（int8），速度会明显变慢")
    return _load(params.model, *CPU_FALLBACK), "cpu"


def _short(e: Exception) -> str:
    return f"{type(e).__name__}: {str(e).strip().splitlines()[0][:160] if str(e).strip() else ''}"


def _read_partial(path: Path, params: dict) -> tuple[str | None, list[dict]]:
    """读取 partial：首行为 {"meta": {...}}，其后每行一个 segment。参数不一致或损坏则作废。"""
    if not path.exists():
        return None, []
    lines = path.read_text(encoding="utf-8").splitlines()
    try:
        meta = json.loads(lines[0])["meta"]
    except (IndexError, KeyError, ValueError, TypeError):
        return None, []
    if meta.get("params") != params:
        return None, []
    segments = []
    for line in lines[1:]:
        try:
            segments.append(json.loads(line))
        except ValueError:
            break  # 中断时写了半行，丢弃其后内容
    return meta.get("language"), segments


def _rewrite_partial(path: Path, language: str | None, params: dict, segments: list[dict]) -> None:
    """重写干净的 partial（去掉可能的半行），后续继续追加。"""
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"meta": {"language": language, "params": params}}, ensure_ascii=False) + "\n")
        for s in segments:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")


def _seg_dict(seg, offset: float) -> dict:
    words = [{"start": round(w.start + offset, 3), "end": round(w.end + offset, 3), "word": w.word}
             for w in (seg.words or [])]
    return {"start": round(seg.start + offset, 3), "end": round(seg.end + offset, 3),
            "text": seg.text.strip(), "words": words}


def _fmt(t: float) -> str:
    t = int(t)
    return f"{t // 3600}:{t // 60 % 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


def _duration(ctx: StageContext, audio: Path) -> float:
    try:
        return float(ctx.read("audio.json")["duration_s"])
    except (FileNotFoundError, KeyError, TypeError, ValueError):
        return probe(audio)["duration_s"] or 0.0


def run(ctx: StageContext) -> None:
    require_enabled(ctx.settings.asr)
    audio = ctx.dir / "audio.wav"
    if not audio.exists():
        raise StageError("找不到 audio.wav，请先完成提取音频阶段")
    duration = _duration(ctx, audio)
    params = ctx.params
    partial_path = ctx.dir / PARTIAL
    resume_wav = ctx.dir / RESUME_WAV
    asr = ctx.settings.asr

    # 出错时逐级退让：GPU 批量 → GPU 顺序 → CPU（int8），已写入的 segment 都保留、从断点续跑
    for attempt in range(3):
        language, segments = _read_partial(partial_path, params)
        _rewrite_partial(partial_path, language, params, segments)
        offset = segments[-1]["end"] if segments else 0.0
        if duration and offset >= duration - 0.5:
            break

        ctx.progress(offset / duration if duration else 0.0,
                     f"加载语音识别模型 {asr.model}（首次使用需下载）" if not segments
                     else f"从 {_fmt(offset)} 处继续识别，加载模型 {asr.model}")
        try:
            model, device = get_model(asr, lambda msg: ctx.progress(offset / duration if duration else 0.0, msg))
        except StageError:
            raise
        except Exception as e:  # noqa: BLE001
            raise StageError(f"无法加载语音识别模型 {asr.model}（首次使用需联网下载）：{_short(e)}") from e

        src = audio
        if offset > 0:
            cut_wav(audio, resume_wav, offset)
            src = resume_wav
        try:
            opts = dict(language=language, vad_filter=True, word_timestamps=True,
                        condition_on_previous_text=False)
            seg_iter, info = transcribe(model, str(src), asr.batch_size, **opts)
            if language is None:
                language = info.language
                _rewrite_partial(partial_path, language, params, segments)
            with partial_path.open("a", encoding="utf-8") as f:
                for seg in seg_iter:
                    d = _seg_dict(seg, offset)
                    f.write(json.dumps(d, ensure_ascii=False) + "\n")
                    f.flush()
                    if duration:
                        ctx.progress(d["end"] / duration, f"已识别 {_fmt(d['end'])} / {_fmt(duration)}")
            break
        except Exception as e:  # noqa: BLE001
            # 只换本地参数，不改 ctx.settings，产物 params 仍记录配置值，避免误判需重跑
            if device != "cpu" and asr.batch_size > 0:
                # 批量识别出错（多为显存不足）：先在 GPU 上改用顺序识别
                asr = replace(asr, batch_size=0)
                ctx.progress(offset / duration if duration else 0.0,
                             f"批量识别出错（{_short(e)}），改用顺序识别继续")
                continue
            if device != "cpu" and attempt < 2:
                # 推理中途 CUDA 出错（如缺 DLL）：改用 CPU 续跑
                asr = replace(asr, device=CPU_FALLBACK[0], compute_type=CPU_FALLBACK[1])
                ctx.progress(offset / duration if duration else 0.0,
                             f"GPU 识别出错（{_short(e)}），改用 CPU（int8）继续")
                continue
            raise StageError(f"语音识别失败：{_short(e)}") from e
        finally:
            resume_wav.unlink(missing_ok=True)

    language, segments = _read_partial(partial_path, params)
    ctx.write("transcript.json", {"language": language or "en", "segments": segments})
    partial_path.unlink(missing_ok=True)
    ctx.progress(1.0, f"识别完成，共 {len(segments)} 段")
