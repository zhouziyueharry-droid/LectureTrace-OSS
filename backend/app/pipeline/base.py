"""阶段接口。每个阶段模块暴露 `run(ctx: StageContext) -> None`。

- 输入输出只通过 ctx.read / ctx.write（带版本的 JSON）或 ctx.dir 下的文件。
- 进度用 ctx.progress(0~1, "说明")，由 runner 转发为 SSE 事件。
- 可预期的失败抛 StageError("给用户看的中文原因")。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .. import artifacts
from ..config import Settings, settings as default_settings


class StageError(Exception):
    """阶段失败，message 会显示在页面上。"""


# 按执行顺序排列；await_confirm 是 runner 处理的暂停点，不是模块
STAGES: list[str] = [
    "extract_audio", "keyframes", "asr", "cues",  # 抽帧不依赖语音，放前面让展示台尽早可用
    "segments",                                   # 本机：切分讲解片段，需要 cues + keyframes
    "await_confirm",
    "translate", "read_slides", "evidence", "outline",
]
# 按名字切片，插入新阶段时不必改下标
LOCAL_STAGES = STAGES[:STAGES.index("await_confirm")]
API_STAGES = STAGES[STAGES.index("await_confirm") + 1:]

# 每个阶段的主产物，用于判断是否需要重跑
STAGE_OUTPUT: dict[str, str] = {
    "extract_audio": "audio.wav",
    "asr": "transcript.json",
    "cues": "cues.json",
    "keyframes": "keyframes.json",
    "segments": "segments.json",
    "translate": "cues.json",          # 就地补 zh，并生成两个 vtt
    "read_slides": "slides.json",
    "evidence": "evidence.json",
    "outline": "outline.json",
}


@dataclass
class StageContext:
    video_id: str
    stage: str
    settings: Settings = field(default_factory=lambda: default_settings)
    on_progress: Callable[[float, str], None] | None = None

    @property
    def dir(self) -> Path:
        return artifacts.video_dir(self.video_id)

    @property
    def source(self) -> Path:
        return self.dir / "source.mp4"

    @property
    def params(self) -> dict:
        return self.settings.stage_params(self.stage)

    def progress(self, fraction: float, message: str = "") -> None:
        if self.on_progress:
            self.on_progress(max(0.0, min(1.0, fraction)), message)

    def read(self, name: str) -> Any:
        return artifacts.read_artifact(self.video_id, name)

    def write(self, name: str, data: Any) -> Path:
        return artifacts.write_artifact(self.video_id, name, data, self.params)
