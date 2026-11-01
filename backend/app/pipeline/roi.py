"""幻灯片区域（ROI）的确认记录与预填建议（提案 0001 §2.5，P9）。

不是阶段模块，也不依赖 OpenCV：runner 和接口直接用它（keyframes 模块在测试里会被假模块替换）。

- 确认记录：app_settings 的 `keyframe_roi_confirmed:<vid>` = {"roi": [x, y, w, h], "confirmed_at": iso}。
  选择"整个画面"时显式保存 [0, 0, 1, 1]，不用 null 表示。
- 预填建议：视频自己上次保存的框 `keyframe_roi:<vid>`，其次全局默认 `keyframe_roi_default`。
- 旧视频（已有 keyframes.json、没有确认记录）视为已按原来的区域（即预填建议）确认。
"""
from __future__ import annotations

from .. import artifacts, db

FULL_FRAME: list[float] = [0.0, 0.0, 1.0, 1.0]
DEFAULT_KEY = "keyframe_roi_default"


def confirmed_key(video_id: str) -> str:
    return f"keyframe_roi_confirmed:{video_id}"


def video_key(video_id: str) -> str:
    return f"keyframe_roi:{video_id}"


def is_full_frame(roi) -> bool:
    return not roi or [float(v) for v in roi] == FULL_FRAME


def confirmed_roi(video_id: str) -> list[float] | None:
    """已确认的区域；没有确认记录时返回 None（整个画面也会返回 [0, 0, 1, 1]）。"""
    rec = db.get_setting(confirmed_key(video_id))
    if isinstance(rec, dict) and isinstance(rec.get("roi"), list):
        return rec["roi"]
    return None


def suggestion(video_id: str) -> list[float] | None:
    """预填建议：视频自己保存过的框，其次全局默认；都没有返回 None（整个画面）。"""
    roi = db.get_setting(video_key(video_id))
    return roi if roi is not None else db.get_setting(DEFAULT_KEY)


def is_confirmed(video_id: str) -> bool:
    if confirmed_roi(video_id) is not None:
        return True
    # 旧视频：抽帧已经按原来的区域跑过，不强制重新框选
    return (artifacts.video_dir(video_id) / "keyframes.json").exists()


def save_confirmation(video_id: str, roi: list[float] | None) -> list[float]:
    """写入确认记录（None 视为整个画面），返回保存的区域。"""
    value = [round(float(v), 4) for v in roi] if roi else list(FULL_FRAME)
    db.set_setting(confirmed_key(video_id), {"roi": value, "confirmed_at": db.now_iso()})
    return value


def effective(video_id: str) -> list[float] | None:
    """抽帧实际使用的区域：已确认的优先，否则用预填建议（旧视频兼容）。整个画面返回 None。"""
    roi = confirmed_roi(video_id)
    if roi is None:
        roi = suggestion(video_id)
    return None if is_full_frame(roi) else roi


def delete(video_id: str) -> None:
    for key in (video_key(video_id), confirmed_key(video_id)):
        db.execute("DELETE FROM app_settings WHERE key = ?", (key,))
