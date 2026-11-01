"""处理流程相关接口：SSE 事件、发送清单、确认、重试。"""
from __future__ import annotations

import importlib
import json
import logging

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from .. import events
from ..pipeline import runner
from .videos import get_video, video_detail

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/videos", tags=["processing"])


class ConfirmBody(BaseModel):
    remember: bool = False


class RetryBody(BaseModel):
    # 可选：重跑指定阶段及其下游（例如传入 stale_stages）；都不传则从第一个未完成阶段继续
    stages: list[str] | None = None
    from_stage: str | None = None


@router.get("/{video_id}/events")
async def video_events(video_id: str):
    """先推送 snapshot，再转发该视频的所有事件。"""
    get_video(video_id)

    async def gen():
        # 先订阅再取快照，避免两者之间的事件丢失
        async with events.subscribe(video_id) as sub:
            try:
                snapshot = video_detail(video_id)
            except HTTPException:  # 视频刚被删除
                return
            yield {"data": json.dumps({"type": "snapshot", "video": snapshot}, ensure_ascii=False)}
            async for ev in sub:
                yield {"data": json.dumps(ev, ensure_ascii=False)}

    return EventSourceResponse(gen(), ping=15)


@router.get("/{video_id}/send-plan")
def send_plan(video_id: str):
    get_video(video_id)
    try:
        mod = importlib.import_module("app.pipeline.send_plan")
    except ImportError:
        raise HTTPException(503, "发送清单模块尚未就绪")
    try:
        return mod.build_send_plan(video_id)
    except FileNotFoundError:
        raise HTTPException(409, "本机阶段尚未完成，暂时无法生成发送清单")
    except Exception as e:
        log.exception("生成发送清单失败（视频 %s）", video_id)
        raise HTTPException(500, f"生成发送清单失败：{e}")


@router.post("/{video_id}/confirm")
def confirm(video_id: str, body: ConfirmBody | None = Body(default=None)):
    get_video(video_id)
    from ..pipeline.send_plan import build_send_plan

    blocking = build_send_plan(video_id)["blocking"]
    if blocking:
        raise HTTPException(409, "；".join(blocking))
    try:
        runner.confirm(video_id, remember=bool(body and body.remember))
    except ValueError as e:
        raise HTTPException(409, str(e))
    return video_detail(video_id)


@router.post("/{video_id}/outline/run")
def run_outline(video_id: str):
    """为该视频运行读幻灯片 → 整理证据 → 生成大纲（会先停在发送确认，显示费用预估）。"""
    get_video(video_id)
    try:
        runner.enable_outline(video_id)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return video_detail(video_id)


@router.post("/{video_id}/retry")
def retry(video_id: str, body: RetryBody | None = Body(default=None)):
    get_video(video_id)
    names = list((body and body.stages) or []) + ([body.from_stage] if body and body.from_stage else [])
    if names:
        if runner.is_active(video_id):
            raise HTTPException(409, "视频正在处理中，请稍后再重跑")
        try:
            runner.reset_stages(video_id, names)
        except ValueError as e:
            raise HTTPException(400, str(e))
    runner.retry(video_id)   # 失败的抽帧改回 pending 后入队
    return video_detail(video_id)
