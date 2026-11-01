"""视频结果目录与带版本的 JSON 产物读写（ARCHITECTURE §5.1）。"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from .config import SCHEMA_VERSION, settings
from .db import now_iso


def video_dir(video_id: str) -> Path:
    return settings.videos_dir / video_id


def write_json(path: Path, obj: Any) -> None:
    """原子写入：先写临时文件再替换，避免中断留下半截文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    # 临时文件名带线程与进程标识：多个线程同时写同一文件时不会互相覆盖临时文件
    tmp = path.with_name(f"{path.name}.{os.getpid()}-{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    # Windows 上目标文件正被其他线程读取时 os.replace 会拒绝访问，稍等重试
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.05 * (attempt + 1))


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_artifact(video_id: str, name: str, data: Any, params: dict) -> Path:
    """name 如 "cues.json"。顶层结构 {schema_version, params, created_at, data}。"""
    path = video_dir(video_id) / name
    write_json(path, {"schema_version": SCHEMA_VERSION, "params": params,
                      "created_at": now_iso(), "data": data})
    return path


def read_artifact(video_id: str, name: str) -> Any:
    """返回 data 字段；文件不存在抛 FileNotFoundError。"""
    return read_json(video_dir(video_id) / name)["data"]


def read_artifact_meta(video_id: str, name: str) -> dict | None:
    path = video_dir(video_id) / name
    if not path.exists():
        return None
    obj = read_json(path)
    return {"schema_version": obj.get("schema_version"), "params": obj.get("params")}
