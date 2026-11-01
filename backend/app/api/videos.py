"""视频的增删查、文件、字幕、大纲、幻灯片接口（ARCHITECTURE §6）。"""
from __future__ import annotations

import re
import shutil
import subprocess
import uuid
from pathlib import Path

import anyio
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from .. import artifacts, db
from ..playback import playback_file
from ..pipeline import keyframes, runner
from ..pipeline import roi as roi_store
from ..pipeline.base import STAGES, StageError

try:
    import python_multipart as multipart
    from python_multipart.multipart import parse_options_header
except ModuleNotFoundError:  # 旧版包名
    import multipart  # type: ignore
    from multipart.multipart import parse_options_header  # type: ignore

router = APIRouter(prefix="/api/videos", tags=["videos"])

VIDEO_ID_RE = re.compile(r"^[0-9a-f]{12}$")
SLIDE_ID_RE = re.compile(r"^S\d{3,6}(?:_\d+)?$")  # 第 k 次出现的截图为 S012_k（交接 §3.7.1）
SUBTITLE_FILES = {"source": "subs.source.vtt", "bilingual": "subs.bilingual.vtt"}


# ---------- 共用工具 ----------

def get_video(video_id: str) -> dict:
    """校验 ID 并取视频行，不存在抛 404。"""
    if not VIDEO_ID_RE.match(video_id):
        raise HTTPException(404, "视频不存在")
    video = db.fetch_one("SELECT * FROM videos WHERE id = ?", (video_id,))
    if video is None:
        raise HTTPException(404, "视频不存在")
    return video


def video_detail(video_id: str) -> dict:
    """GET /api/videos/{id} 与 SSE snapshot 共用的结构。"""
    video = get_video(video_id)
    rows = {r["name"]: r for r in db.fetch_all("SELECT * FROM stages WHERE video_id = ?", (video_id,))}
    stages = []
    for name in STAGES:
        r = rows.get(name) or {"status": "pending", "started_at": None, "finished_at": None,
                               "error": None, "attempt": 0}
        stages.append({"name": name, "status": r["status"], "started_at": r["started_at"],
                       "finished_at": r["finished_at"], "error": r["error"], "attempt": r["attempt"]})
    try:
        stale = runner.stale_stages(video_id)
    except Exception:
        stale = []
    confirmed = roi_store.is_confirmed(video_id)
    return {**video, "stages": stages, "stale_stages": stale,
            # 幻灯片区域（0001 §2.5）：roi 为当前使用的区域（未确认时为 null，整个画面也为 null）
            "roi_confirmed": confirmed,
            "roi": roi_store.effective(video_id) if confirmed else None,
            "roi_suggestion": roi_store.suggestion(video_id)}


def _artifact_data(video_id: str, name: str, missing: str):
    get_video(video_id)
    try:
        return artifacts.read_artifact(video_id, name)
    except FileNotFoundError:
        raise HTTPException(404, missing)


# ---------- 上传 ----------

class _UploadError(Exception):
    pass


def _decode_filename(raw: bytes) -> str:
    """浏览器发 UTF-8；Windows 下的 curl 等工具可能发 GBK。"""
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


@router.post("")
async def upload_video(request: Request):
    """multipart 流式写盘；只接受 .mp4，字段名不限（取第一个文件部分）。"""
    ctype, params = parse_options_header(request.headers.get("content-type", ""))
    if ctype != b"multipart/form-data" or b"boundary" not in params:
        raise HTTPException(400, "请以 multipart/form-data 上传 MP4 文件")

    video_id = uuid.uuid4().hex[:12]
    vdir = artifacts.video_dir(video_id)
    vdir.mkdir(parents=True, exist_ok=True)
    part_path = vdir / "source.mp4.part"

    state = {"headers": {}, "hname": b"", "hval": b"", "is_file": False, "filename": None,
             "done": False}
    pending: list[bytes] = []   # 本轮待写入的文件数据

    def on_part_begin():
        state.update(headers={}, hname=b"", hval=b"", is_file=False)

    def on_header_field(data, start, end):
        state["hname"] += data[start:end]

    def on_header_value(data, start, end):
        state["hval"] += data[start:end]

    def on_header_end():
        state["headers"][state["hname"].lower()] = state["hval"]
        state["hname"], state["hval"] = b"", b""

    def on_headers_finished():
        _, opts = parse_options_header(state["headers"].get(b"content-disposition", b""))
        if b"filename" in opts and state["filename"] is None:
            name = _decode_filename(opts[b"filename"])
            name = name.replace("\\", "/").rsplit("/", 1)[-1]
            if not name.lower().endswith(".mp4"):
                raise _UploadError("只支持 MP4 文件")
            state["filename"], state["is_file"] = name, True

    def on_part_data(data, start, end):
        if state["is_file"]:
            pending.append(bytes(data[start:end]))

    def on_part_end():
        if state["is_file"]:
            state["is_file"], state["done"] = False, True

    parser = multipart.MultipartParser(params[b"boundary"], {
        "on_part_begin": on_part_begin, "on_part_data": on_part_data, "on_part_end": on_part_end,
        "on_header_field": on_header_field, "on_header_value": on_header_value,
        "on_header_end": on_header_end, "on_headers_finished": on_headers_finished,
    })

    def write_chunks(f, chunks):
        for c in chunks:
            f.write(c)

    try:
        with open(part_path, "wb") as f:
            async for chunk in request.stream():
                parser.write(chunk)
                if pending:
                    chunks = pending[:]
                    pending.clear()
                    await anyio.to_thread.run_sync(write_chunks, f, chunks)
            parser.finalize()
        if not state["done"]:
            raise _UploadError("请求中没有找到 MP4 文件")
        part_path.replace(vdir / "source.mp4")
    except _UploadError as e:
        shutil.rmtree(vdir, ignore_errors=True)
        raise HTTPException(400, str(e))
    except Exception:  # 格式错误、客户端断开等
        shutil.rmtree(vdir, ignore_errors=True)
        raise HTTPException(400, "上传失败：请求数据不完整或格式错误")
    except BaseException:  # 任务被取消
        shutil.rmtree(vdir, ignore_errors=True)
        raise

    filename = state["filename"]
    title = Path(filename).stem or filename
    db.execute("INSERT INTO videos (id, title, filename, duration_s, created_at, status)"
               " VALUES (?, ?, ?, NULL, ?, 'processing')", (video_id, title, filename, db.now_iso()))
    runner.init_stages(video_id)
    runner.enqueue(video_id)
    return {"video_id": video_id, **get_video(video_id)}


# ---------- 查询与删除 ----------

@router.get("")
def list_videos():
    return db.fetch_all("SELECT id, title, filename, duration_s, created_at, status, course_id FROM videos"
                        " ORDER BY created_at DESC, rowid DESC")


@router.get("/{video_id}")
def get_video_detail(video_id: str):
    return video_detail(video_id)


class VideoCourseBody(BaseModel):
    course_id: str | None


@router.put("/{video_id}/course")
def set_video_course(video_id: str, body: VideoCourseBody):
    if not VIDEO_ID_RE.fullmatch(video_id):
        raise HTTPException(404, "视频不存在")
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM videos WHERE id = ?", (video_id,)).fetchone():
            raise HTTPException(404, "视频不存在")
        if body.course_id is not None and not conn.execute(
            "SELECT 1 FROM courses WHERE id = ?", (body.course_id,)
        ).fetchone():
            raise HTTPException(404, "课程不存在")
        conn.execute("UPDATE videos SET course_id = ? WHERE id = ?", (body.course_id, video_id))
    return video_detail(video_id)


@router.delete("/{video_id}")
def delete_video(video_id: str):
    get_video(video_id)
    runner.cancel(video_id)
    from ..qa import codex_chat, chat_runs, conversations, notes
    run = chat_runs.stop(video_id)
    if run and not run.finished.wait(15):
        raise HTTPException(409, '正在停止回答，请稍后再删除视频')
    codex_chat.stop(video_id)
    with codex_chat.conversation_lock(video_id), notes.lock(video_id):
        if chat_runs.is_running(video_id):
            raise HTTPException(409, '这节课正在回答，请停止后再删除视频')
        conversations.delete_video_locked(video_id)
        db.execute("DELETE FROM stages WHERE video_id = ?", (video_id,))
        db.execute("DELETE FROM videos WHERE id = ?", (video_id,))
        db.execute("DELETE FROM app_settings WHERE key = ?", (f"outline_run:{video_id}",))
        roi_store.delete(video_id)
        # 正在处理时文件可能被占用，runner 结束该视频后会再清理一次
        shutil.rmtree(artifacts.video_dir(video_id), ignore_errors=True)
    return {"ok": True}


# ---------- 文件类 ----------

@router.get("/{video_id}/file")
def get_video_file(video_id: str):
    """Starlette FileResponse 自带 Range 支持（206 / Content-Range / Accept-Ranges）。"""
    get_video(video_id)
    path = artifacts.video_dir(video_id) / "source.mp4"
    if not path.is_file():
        raise HTTPException(404, "视频文件不存在")
    try:
        path = playback_file(path)
    except StageError as e:
        raise HTTPException(503, str(e)) from e
    return FileResponse(path, media_type="video/mp4")


@router.get("/{video_id}/subtitles/{kind}.vtt")
def get_subtitles(video_id: str, kind: str):
    get_video(video_id)
    if kind not in SUBTITLE_FILES:
        raise HTTPException(404, "字幕不存在")
    path = artifacts.video_dir(video_id) / SUBTITLE_FILES[kind]
    if not path.is_file():
        raise HTTPException(404, "字幕尚未生成")
    return FileResponse(path, media_type="text/vtt; charset=utf-8",
                        headers={"Cache-Control": "no-cache"})


@router.get("/{video_id}/outline")
def get_outline(video_id: str):
    from ..qa import outline_edits
    get_video(video_id)
    try:
        return outline_edits.ensure(video_id)
    except FileNotFoundError:
        raise HTTPException(404, "大纲尚未生成") from None
    except outline_edits.OutlineEditError as error:
        raise HTTPException(error.status_code, str(error)) from error


@router.get("/{video_id}/slides")
def get_slides(video_id: str):
    return _artifact_data(video_id, "slides.json", "幻灯片记录尚未生成")


@router.get("/{video_id}/slides/{slide_id}.jpg")
def get_slide_image(video_id: str, slide_id: str):
    get_video(video_id)
    if not SLIDE_ID_RE.match(slide_id):
        raise HTTPException(404, "幻灯片不存在")
    path = artifacts.video_dir(video_id) / "keyframes" / f"{slide_id}.jpg"
    if not path.is_file():
        raise HTTPException(404, "幻灯片不存在")
    # 重跑抽帧会覆盖同名图片，不能长期缓存
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})


@router.get("/{video_id}/keyframes")
def get_keyframes(video_id: str):
    """抽帧展示台：幻灯片列表 + 帧差曲线等调试数据 + 当时使用的参数。"""
    slides = _artifact_data(video_id, "keyframes.json", "尚未提取幻灯片")
    meta = artifacts.read_artifact_meta(video_id, "keyframes.json") or {}
    try:
        debug = artifacts.read_artifact(video_id, "keyframes_debug.json")
    except FileNotFoundError:
        debug = None  # 旧版本抽帧没有调试数据
    return {"slides": slides, "debug": debug, "params": meta.get("params"),
            "roi": roi_store.effective(video_id), "roi_default": db.get_setting(roi_store.DEFAULT_KEY)}


class RoiBody(BaseModel):
    roi: list[float] | None = None   # 相对坐标 [x, y, w, h]；None 表示改用全局默认
    as_default: bool = False         # 同时设为以后导入视频的默认区域
    rerun: bool = True               # 已抽过帧时，保存后按新区域重新抽帧
    confirm: bool = True             # 写入确认记录（0001 §2.5）；未抽过帧时随即开始抽帧
    full_frame: bool = False         # 使用整个画面（忽略 roi，保存为 [0, 0, 1, 1]）


@router.put("/{video_id}/keyframes/roi")
def put_keyframe_roi(video_id: str, body: RoiBody):
    """保存幻灯片区域。confirm=true 时写入确认记录并启动抽帧（未抽过）或重新抽帧（已抽过、rerun=true）；
    confirm=false 只保存为预填建议，不抽帧。返回视频详情。"""
    get_video(video_id)
    if body.full_frame:
        roi: list[float] | None = list(roi_store.FULL_FRAME)
    elif body.roi is not None:
        if not keyframes.valid_roi(body.roi):
            raise HTTPException(400, "幻灯片区域无效：需在画面内，且宽高都不小于画面的 5%")
        roi = [round(v, 4) for v in body.roi]
    else:
        roi = None
    key = roi_store.video_key(video_id)
    if roi is None:
        db.execute("DELETE FROM app_settings WHERE key = ?", (key,))
    else:
        db.set_setting(key, roi)
    if body.as_default and roi is not None:
        db.set_setting(roi_store.DEFAULT_KEY, roi)
    if body.confirm:
        # roi 为 None（改用全局默认）时，确认的是此刻的默认值；都没有则为整个画面
        effective = roi if roi is not None else roi_store.suggestion(video_id)
        try:
            # 抽帧不依赖其他阶段：即使视频正在做语音识别，也能立即开始或按新区域重跑
            runner.confirm_roi(video_id, effective, rerun=body.rerun)
        except ValueError as e:
            raise HTTPException(409, str(e))
    return video_detail(video_id)


@router.get("/{video_id}/frame")
def get_frame(video_id: str, t: float, w: int = 480):
    """截取任意时刻的一帧（JPEG），用于在展示台核对算法没切到的位置。"""
    get_video(video_id)
    path = artifacts.video_dir(video_id) / "source.mp4"
    if not path.is_file():
        raise HTTPException(404, "视频文件不存在")
    w = max(120, min(1280, w))
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", str(path), "-frames:v", "1",
           "-vf", f"scale={w}:-2", "-q:v", "4", "-f", "image2pipe", "-vcodec", "mjpeg", "-"]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=20, check=True).stdout
    except (subprocess.SubprocessError, OSError) as e:
        raise HTTPException(500, f"截取画面失败：{e}")
    if not out:
        raise HTTPException(404, "该时刻没有画面")
    return Response(out, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})
