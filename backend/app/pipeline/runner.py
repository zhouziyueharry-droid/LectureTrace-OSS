"""阶段编排：单个后台工作线程按顺序处理队列中的视频（ARCHITECTURE §3）。

- 每个阶段更新 stages 表与 videos.status，并通过 events.publish 推送事件。
- 到 await_confirm 暂停，confirm() 后继续；设置 skip_confirm 时自动跳过。
- 失败只记录原因，不删除已有产物；重试从第一个非 done/skipped 的阶段开始。
- 阶段模块按名称惰性导入（app.pipeline.<module>），模块不存在时该阶段失败。
- 幻灯片区域（ROI）未确认时不抽帧，语音识别照常；主循环到汇合点（segments）仍未确认时离开，
  视频进入 awaiting_roi 并记入 _parked，由抽帧线程结束时叫回（交接 0001 §2.5）。
"""
from __future__ import annotations

import importlib
import json
import logging
import queue
import shutil
import threading
import time
from typing import Any, Callable, Iterable

from .. import artifacts, config, db, events
from ..config import SCHEMA_VERSION
from . import roi as roi_store
from .base import API_STAGES, STAGE_OUTPUT, STAGES, StageContext, StageError

log = logging.getLogger(__name__)

AWAIT = "await_confirm"
DISABLED_REASON = "未开启大纲生成"
# 需要调用 GPT 生成大纲的三个阶段：默认不执行，由设置页开关或视频页"运行大纲生成"开启。
# segments 是 await_confirm 之前的本机阶段，不在其中：即使未开启大纲也要执行
OUTLINE_STAGES = ("read_slides", "evidence", "outline")
ROI_WAIT_MESSAGE = "等待确认幻灯片区域"


def is_disabled(video_id: str, name: str) -> bool:
    if name in config.settings.disabled_stages:
        return True
    if name in OUTLINE_STAGES:
        from ..llm import settings as llm_settings

        return not llm_settings.outline_enabled_for(video_id)
    return False


def enable_outline(video_id: str) -> list[str]:
    """该视频单独开启大纲生成：重置 7～9 步（需重新确认发送）并入队。返回被重置的阶段。"""
    if is_active(video_id):
        raise ValueError("视频正在处理中，请稍后再运行")
    db.set_setting(f"outline_run:{video_id}", True)
    targets = reset_stages(video_id, list(OUTLINE_STAGES))
    enqueue(video_id)
    return targets

# 阶段名 -> app.pipeline 下的模块名
STAGE_MODULES: dict[str, str] = {
    "extract_audio": "audio",
    "asr": "asr",
    "cues": "cues",
    "keyframes": "keyframes",
    "segments": "segments",
    "translate": "translate",
    "read_slides": "slides",
    "evidence": "evidence",
    "outline": "outline",
}

# 数据依赖：阶段 -> 直接上游（依据各阶段读取的产物），用于确定"下游"
DEPENDS: dict[str, list[str]] = {
    "extract_audio": [],
    "asr": ["extract_audio"],
    "cues": ["asr"],
    "keyframes": [],
    "segments": ["cues", "keyframes"],
    "translate": ["cues"],
    "read_slides": ["segments"],
    "evidence": ["segments", "read_slides"],
    "outline": ["evidence"],
}

# runner 自己记录的各阶段完成时参数快照（用于非 JSON 主产物或多个阶段共用同一产物的情况）
STAGE_META_FILE = "stage_meta.json"

PROGRESS_MIN_INTERVAL_S = 0.5
PROGRESS_MIN_STEP = 0.01

_queue: "queue.Queue[str]" = queue.Queue()
_lock = threading.Lock()
_queued: set[str] = set()       # 已在队列中、尚未开始
_current: str | None = None     # 正在处理的视频
_cancelled: set[str] = set()    # 已删除、需跳过的视频
_rerun: set[str] = set()        # 处理中又被 enqueue 的视频，本轮结束后重新入队
_parked: set[str] = set()       # 停在 awaiting_roi 的视频：抽帧线程结束时叫回（只在内存，重启由 recover 恢复）
_worker: threading.Thread | None = None

# 测试钩子：在关键时间点调用 f(video_id)，用于构造竞争时序。生产代码中为空
_hooks: dict[str, Callable[[str], None]] = {}


def _hook(point: str, video_id: str) -> None:
    f = _hooks.get(point)
    if f is not None:
        f(video_id)


# ---------- 数据库小工具 ----------

def _set_stage(video_id: str, name: str, **fields: Any) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    db.execute(f"UPDATE stages SET {cols} WHERE video_id = ? AND name = ?",
               (*fields.values(), video_id, name))


def _stage_rows(video_id: str) -> dict[str, dict]:
    rows = db.fetch_all("SELECT * FROM stages WHERE video_id = ?", (video_id,))
    return {r["name"]: r for r in rows}


def _set_video_status(video_id: str, status: str) -> None:
    db.execute("UPDATE videos SET status = ? WHERE id = ?", (status, video_id))
    events.publish(video_id, {"type": "video", "status": status})


def _stage_event(video_id: str, name: str, status: str, progress: float = 0.0,
                 message: str = "", error: str | None = None) -> None:
    events.publish(video_id, {"type": "stage", "stage": name, "status": status,
                              "progress": progress, "message": message, "error": error})


def _is_cancelled(video_id: str) -> bool:
    with _lock:
        return video_id in _cancelled


# ---------- 公共接口 ----------

def init_stages(video_id: str) -> None:
    """为新视频建立全部阶段行（pending）；已存在的行不动。"""
    for name in STAGES:
        db.execute("INSERT OR IGNORE INTO stages (video_id, name, status, attempt) VALUES (?, ?, 'pending', 0)",
                   (video_id, name))


def enqueue(video_id: str) -> None:
    """加入处理队列，从第一个未完成阶段开始；已在队列或正在处理则忽略。"""
    with _lock:
        _cancelled.discard(video_id)
        if video_id == _current:
            _rerun.add(video_id)  # 工作线程可能正要退出本轮，结束后再跑一遍
        if video_id in _queued or video_id == _current:
            return
        _queued.add(video_id)
    init_stages(video_id)
    _set_video_status(video_id, "processing")
    _queue.put(video_id)


def confirm(video_id: str, remember: bool = False) -> None:
    """用户确认发送：结束 await_confirm 并继续外部 API 阶段。不在等待确认时抛 ValueError。"""
    row = _stage_rows(video_id).get(AWAIT)
    if not row or row["status"] != "running":
        raise ValueError("当前不在等待确认状态")
    if remember:
        db.set_setting("skip_confirm", True)
    _set_stage(video_id, AWAIT, status="done", finished_at=db.now_iso(), error=None)
    _stage_event(video_id, AWAIT, "done", 1.0)
    enqueue(video_id)


def retry(video_id: str) -> None:
    """重试：失败的独立线程阶段（抽帧）改回 pending，再从第一个未完成阶段继续。

    主循环不会自动重跑失败的抽帧（否则抽帧失败后叫回主循环时会被悄悄重跑，视频永远不会标为失败）。
    """
    with _lock:
        busy = {n for (v, n) in _side_jobs if v == video_id}
    rows = _stage_rows(video_id)
    for name in CONCURRENT_STAGES:
        if name not in busy and rows.get(name, {}).get("status") == "failed":
            _set_stage(video_id, name, status="pending", started_at=None, finished_at=None, error=None)
    enqueue(video_id)


def confirm_roi(video_id: str, roi: list[float] | None, rerun: bool = True) -> str:
    """用户确认幻灯片区域（None 表示整个画面）：写确认记录，然后只负责启动抽帧（交接 §2.5 第 2 步）。

    不入队、不看视频状态：主循环停在 awaiting_roi 时，由抽帧线程结束时叫回（见 _start_side）。
    返回 "started"（已在独立线程开始或已在运行）、"queued"（交给主队列）或 "saved"（只保存）。
    抽帧正在运行时抛 ValueError，不写记录。
    """
    status = _stage_rows(video_id).get("keyframes", {}).get("status", "pending")
    with _lock:
        busy = (video_id, "keyframes") in _side_jobs
    # 线程已登记但尚未开始（仍是 pending）时可以改：它开始后才读取区域
    if status == "running" or (busy and status != "pending"):
        raise ValueError("正在抽帧，请等这次抽帧结束后再修改区域")
    roi_store.save_confirmation(video_id, roi)
    _hook("roi_recorded", video_id)
    if status in ("done", "failed"):
        # 已经抽过帧：视为修改区域，只重跑抽帧及其下游（第 6 步）
        return rerun_independent_stage(video_id, "keyframes") if rerun else "saved"
    if status == "skipped":
        return "saved"
    # pending：立即在独立线程开始；已在运行时 _start_side 返回 False，没有副作用
    _start_side(video_id, "keyframes", unless_done=True)
    return "started"


def downstream(names: Iterable[str]) -> list[str]:
    """给定阶段及其全部下游（按 STAGES 顺序，不含 await_confirm）。"""
    result = set(names)
    for name in STAGES:  # STAGES 已按拓扑顺序排列，一遍即可
        if any(dep in result for dep in DEPENDS.get(name, [])):
            result.add(name)
    return [n for n in STAGES if n in result and n != AWAIT]


def reset_stages(video_id: str, names: Iterable[str]) -> list[str]:
    """把指定阶段及其下游重置为 pending（参数变化后重跑）；涉及外部 API 阶段时需重新确认发送。"""
    names = list(names)
    unknown = [n for n in names if n not in DEPENDS]
    if unknown:
        raise ValueError(f"未知阶段：{', '.join(unknown)}")
    targets = downstream(names)
    if any(n in API_STAGES and not is_disabled(video_id, n) for n in targets):
        targets.append(AWAIT)
    for name in targets:
        _set_stage(video_id, name, status="pending", started_at=None, finished_at=None, error=None)
    return targets


def reset_from(video_id: str, stage: str) -> list[str]:
    return reset_stages(video_id, [stage])


def cancel(video_id: str) -> bool:
    """删除视频前调用：标记取消。返回该视频此刻是否正在被处理。"""
    with _lock:
        if video_id in _queued or video_id == _current:
            _cancelled.add(video_id)
        _rerun.discard(video_id)
        _parked.discard(video_id)
        return video_id == _current


def is_active(video_id: str) -> bool:
    with _lock:
        return (video_id in _queued or video_id == _current
                or any(v == video_id for v, _ in _side_jobs))


def recover() -> None:
    """进程重启后：残留 running 的阶段改回 pending（await_confirm 保持等待），processing 的视频重新入队。

    awaiting_roi 的视频（_parked 只在内存，重启后丢失）：区域已确认的重新入队，未确认的放回 _parked。
    """
    db.execute("UPDATE stages SET status = 'pending', started_at = NULL "
               "WHERE status = 'running' AND name != ?", (AWAIT,))
    for row in db.fetch_all("SELECT id FROM videos WHERE status = 'awaiting_roi' ORDER BY created_at"):
        if roi_store.is_confirmed(row["id"]):
            enqueue(row["id"])
        else:
            with _lock:
                _parked.add(row["id"])
    for row in db.fetch_all("SELECT id FROM videos WHERE status = 'processing' ORDER BY created_at"):
        enqueue(row["id"])


def start_worker() -> None:
    """启动唯一的后台工作线程（重复调用无副作用），并恢复中断的任务。"""
    global _worker
    with _lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_worker_loop, name="pipeline-runner", daemon=True)
        _worker.start()
    recover()


def wait_idle(timeout: float = 10.0) -> bool:
    """等待队列清空且没有正在处理的视频（主要给测试用）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _lock:
            if not _queued and _current is None and not _side_jobs:
                return True
        time.sleep(0.02)
    return False


# ---------- 工作线程 ----------

def _worker_loop() -> None:
    global _current
    while True:
        video_id = _queue.get()
        with _lock:
            _queued.discard(video_id)
            if video_id in _cancelled:
                _cancelled.discard(video_id)
                continue
            _current = video_id
        try:
            _process(video_id)
        except Exception:  # 兜底，保证线程不退出
            log.exception("处理视频 %s 时出现未捕获异常", video_id)
        finally:
            with _lock:
                _current = None
                cancelled = video_id in _cancelled
                _cancelled.discard(video_id)
                if video_id in _rerun:
                    _rerun.discard(video_id)
                    if not cancelled and video_id not in _queued:
                        _queued.add(video_id)
                        _queue.put(video_id)
            if cancelled:  # 删除时阶段可能仍在写文件，这里再清理一次
                shutil.rmtree(artifacts.video_dir(video_id), ignore_errors=True)


def _process(video_id: str) -> None:
    video = db.fetch_one("SELECT status FROM videos WHERE id = ?", (video_id,))
    if video is None:
        return
    if video["status"] != "processing":  # 经 _rerun 重新入队时状态可能还没改
        _set_video_status(video_id, "processing")
    init_stages(video_id)
    for name in STAGES:
        if _is_cancelled(video_id):
            return
        row = _stage_rows(video_id)[name]
        status = row["status"]
        disabled = is_disabled(video_id, name)
        if status == "skipped" and row["error"] in (DISABLED_REASON, "暂未启用") and not disabled:
            status = "pending"  # 之前因未启用而跳过，现已启用
        if status in ("done", "skipped"):
            continue
        if disabled:
            _set_stage(video_id, name, status="skipped", finished_at=db.now_iso(), error=DISABLED_REASON)
            _stage_event(video_id, name, "skipped", 1.0, DISABLED_REASON, DISABLED_REASON)
            continue
        if name in CONCURRENT_STAGES:
            if status != "pending":
                # failed 留给汇合点把视频标为失败；不在这里自动重跑（重跑由 retry() 改回 pending）
                continue
            if name == "keyframes" and not roi_store.is_confirmed(video_id):
                # 区域未确认：先不抽帧，语音识别照常；确认后由 confirm_roi 启动
                _stage_event(video_id, name, "pending", 0.0, ROI_WAIT_MESSAGE)
                continue
            _start_side(video_id, name, unless_done=True)  # 与后面的语音识别同时进行
            continue
        if name == AWAIT or any(d in CONCURRENT_STAGES for d in DEPENDS.get(name, [])):
            # 汇合点：抽帧必须已完成；区域未确认时离开主循环（awaiting_roi）
            if not _keyframes_gate(video_id):
                return
        if name == AWAIT:
            # "以后不再询问"只对不花钱的发送生效；这次要调用 GPT 时一律停下，先看费用预估
            gpt_pending = any(not is_disabled(video_id, n) and _stage_rows(video_id)[n]["status"] != "done"
                              for n in ("read_slides", "outline"))
            # Google 的免费余额未知，不能沿用 DeepL 免费版的自动确认。
            gpt_pending = gpt_pending or (config.settings.translation_provider() == 'google'
                                         and _stage_rows(video_id)['translate']['status'] != 'done')
            if db.get_setting("skip_confirm", False) and not gpt_pending:
                _set_stage(video_id, AWAIT, status="skipped", finished_at=db.now_iso(), error=None)
                _stage_event(video_id, AWAIT, "skipped", 1.0)
                continue
            if status != "running":
                _set_stage(video_id, AWAIT, status="running", started_at=db.now_iso(),
                           finished_at=None, error=None)
            _stage_event(video_id, AWAIT, "running", 0.0, "等待确认发送")
            _set_video_status(video_id, "awaiting_confirm")
            return
        if not _run_stage(video_id, name):
            return
    if not _join_side(video_id):
        return
    if not _is_cancelled(video_id):
        _set_video_status(video_id, "done")


def _load_stage(name: str):
    return importlib.import_module(f"{__package__}.{STAGE_MODULES[name]}")


def _make_progress(video_id: str, name: str):
    """节流的进度回调：距上次 ≥0.5 秒或进度变化 ≥1% 才推送。"""
    last = {"t": 0.0, "p": -1.0}

    def on_progress(fraction: float, message: str = "") -> None:
        now = time.monotonic()
        if (now - last["t"] >= PROGRESS_MIN_INTERVAL_S or fraction - last["p"] >= PROGRESS_MIN_STEP
                or fraction >= 1.0):
            last["t"], last["p"] = now, fraction
            _stage_event(video_id, name, "running", fraction, message)

    return on_progress


def _run_stage(video_id: str, name: str, fail_video: bool = True) -> bool:
    """执行单个阶段，成功返回 True。fail_video=False 时失败只记在该阶段上（独立线程重跑用）。"""
    row = _stage_rows(video_id)[name]
    _set_stage(video_id, name, status="running", started_at=db.now_iso(), finished_at=None,
               error=None, attempt=(row["attempt"] or 0) + 1)
    _stage_event(video_id, name, "running", 0.0)
    ctx = StageContext(video_id=video_id, stage=name, settings=config.settings,
                       on_progress=_make_progress(video_id, name))
    try:
        try:
            module = _load_stage(name)
        except ImportError as e:
            raise StageError(f"阶段模块 {STAGE_MODULES[name]} 不可用：{e}") from e
        module.run(ctx)
    except StageError as e:
        _fail(video_id, name, str(e) or "阶段失败", fail_video)
        return False
    except Exception as e:
        log.exception("阶段 %s 失败（视频 %s）", name, video_id)
        _fail(video_id, name, f"内部错误：{type(e).__name__}: {e}", fail_video)
        return False

    if _is_cancelled(video_id):
        return False
    if name == "extract_audio":
        _update_duration(video_id)
    _record_stage_meta(video_id, name)
    _set_stage(video_id, name, status="done", finished_at=db.now_iso(), error=None)
    _stage_event(video_id, name, "done", 1.0)
    return True


def _fail(video_id: str, name: str, error: str, fail_video: bool = True) -> None:
    if _is_cancelled(video_id):
        return
    _set_stage(video_id, name, status="failed", finished_at=db.now_iso(), error=error)
    _stage_event(video_id, name, "failed", 0.0, error=error)
    if fail_video:
        _set_video_status(video_id, "failed")


# ---------- 独立线程重跑（抽帧展示台调参） ----------

# 与主队列并行执行的阶段：没有上游依赖、且用的资源不同（抽帧用 CPU 解码，语音识别用 GPU）
CONCURRENT_STAGES = ("keyframes",)

# (video_id, 阶段) -> 完成时 set 的 Event
_side_jobs: dict[tuple[str, str], threading.Event] = {}


def _start_side(video_id: str, name: str, unless_done: bool = False) -> bool:
    """在独立线程执行阶段；同一阶段已在执行时返回 False。失败只记在该阶段上，由主线程汇总。

    unless_done=True：该阶段已完成时也返回 False。判断在 _lock 内进行，
    避免确认区域与主循环几乎同时启动抽帧时，前一次刚跑完又被重跑一遍。
    结束时（成功或失败）如果视频停在 _parked，把它移出并重新入队（交接 §2.5 第 4 步）。
    """
    key = (video_id, name)
    with _lock:
        if key in _side_jobs:
            return False
        if unless_done:
            row = _stage_rows(video_id).get(name)
            if row is not None and row["status"] == "done":
                return False
        done = _side_jobs[key] = threading.Event()

    def work() -> None:
        wake = False
        try:
            _run_stage(video_id, name, fail_video=False)
        finally:
            # 视频在抽帧途中被删除：删除接口清理目录后这里可能又写了文件，再清理一次
            if db.fetch_one("SELECT 1 FROM videos WHERE id = ?", (video_id,)) is None:
                shutil.rmtree(artifacts.video_dir(video_id), ignore_errors=True)
            # 移出 _side_jobs 与检查 _parked 在同一次持锁内完成，和 _keyframes_gate 的判断互斥
            with _lock:
                _side_jobs.pop(key, None)
                if video_id in _parked:
                    _parked.discard(video_id)
                    wake = True
            done.set()
            if wake:  # _lock 不可重入，出锁后再入队
                enqueue(video_id)

    threading.Thread(target=work, name=f"side-{name}-{video_id}", daemon=True).start()
    return True


def _keyframes_gate(video_id: str) -> bool:
    """主循环到汇合点（依赖抽帧的阶段）时决定去留；返回 True 继续，False 离开主循环。

    在 _lock 内一次性读取抽帧状态、_side_jobs 与确认记录（交接 §2.5 第 3 步）：
    - 正在跑：出锁后等待；已完成：继续；失败：视频标为失败；
    - 已确认但还没开始：主循环自己启动再等待；
    - 未确认：记入 _parked，视频状态设为 awaiting_roi，离开。抽帧线程结束时会叫回。
    """
    name = "keyframes"
    with _lock:
        running = (video_id, name) in _side_jobs
        status = _stage_rows(video_id).get(name, {}).get("status", "pending")
        if running:
            action = "join"
        elif status in ("done", "skipped", "failed"):
            action = status
        elif roi_store.is_confirmed(video_id):
            action = "start"
        else:
            _parked.add(video_id)
            action = "park"
    if action == "park":
        _hook("gate_parked", video_id)
        if not _is_cancelled(video_id):
            _stage_event(video_id, name, "pending", 0.0, ROI_WAIT_MESSAGE)
            _set_video_status(video_id, "awaiting_roi")
        return False
    if action == "start":
        _start_side(video_id, name, unless_done=True)  # 被别人抢先启动时返回 False，下面照样等待
    return _join_side(video_id, [name])


def _join_side(video_id: str, names: Iterable[str] | None = None) -> bool:
    """等待该视频在独立线程里执行的阶段结束；其中有失败的返回 False（并把视频标为失败）。"""
    with _lock:
        jobs = [(n, ev) for (v, n), ev in _side_jobs.items() if v == video_id and (names is None or n in names)]
    for _, ev in jobs:
        ev.wait()
    rows = _stage_rows(video_id)
    watched = [n for n in CONCURRENT_STAGES if names is None or n in names]
    if any(rows[n]["status"] == "failed" for n in watched):
        if not _is_cancelled(video_id):
            _set_video_status(video_id, "failed")
        return False
    return True


def rerun_independent_stage(video_id: str, name: str) -> str:
    """重跑一个没有上游依赖的本机阶段（目前只有 keyframes），不必等主队列里的其他阶段。

    返回 "queued"（交给主队列）或 "started"（已在独立线程开始）。该阶段正在运行时抛 ValueError。
    - 视频不在处理中：重置后入队，按正常流程执行；
    - 视频正在处理且主队列已经越过该阶段（例如正在语音识别）：在独立线程里立即执行；
    - 主队列还没轮到该阶段：只重置，主队列到时会用新参数执行。
    """
    if DEPENDS.get(name):
        raise ValueError(f"{name} 依赖其他阶段，不能单独重跑")
    before = _stage_rows(video_id)[name]["status"]
    with _lock:
        if before == "running" or (video_id, name) in _side_jobs:
            raise ValueError("该阶段正在运行，请稍后再试")
    reset_stages(video_id, [name])
    if not is_active(video_id):
        enqueue(video_id)
        return "queued"
    if before == "pending":
        return "queued"
    if not _start_side(video_id, name):
        raise ValueError("该阶段正在运行，请稍后再试")
    return "started"


def _update_duration(video_id: str) -> None:
    try:
        duration = float(artifacts.read_artifact(video_id, "audio.json")["duration_s"])
    except Exception as e:
        log.warning("读取 audio.json 时长失败（视频 %s）：%s", video_id, e)
        return
    db.execute("UPDATE videos SET duration_s = ? WHERE id = ?", (duration, video_id))


# ---------- 参数变化检测 ----------

def _norm(obj: Any) -> Any:
    """统一成 JSON 往返后的形态（tuple→list 等），便于比较。"""
    return json.loads(json.dumps(obj, ensure_ascii=False))


_meta_lock = threading.Lock()  # 抽帧与语音识别并行完成时，stage_meta.json 的读改写需串行


def _record_stage_meta(video_id: str, name: str) -> None:
    path = artifacts.video_dir(video_id) / STAGE_META_FILE
    with _meta_lock:
        try:
            meta = artifacts.read_json(path) if path.exists() else {}
        except Exception:
            meta = {}
        meta[name] = {"schema_version": SCHEMA_VERSION, "params": _norm(config.settings.stage_params(name))}
        artifacts.write_json(path, meta)


def _owned_json_output(name: str) -> str | None:
    """该阶段独占的 JSON 主产物；非 JSON（audio.wav）或多个阶段共用（cues.json）时返回 None。"""
    output = STAGE_OUTPUT.get(name, "")
    if not output.endswith(".json") or list(STAGE_OUTPUT.values()).count(output) > 1:
        return None
    return output


def stale_stages(video_id: str) -> list[str]:
    """参数或 schema_version 与当前配置不一致的已完成阶段及其下游（按 DEPENDS，不含 await_confirm）。

    依据两处记录（有哪个比哪个，任一不一致即过期）：
    - 阶段独占的主产物（base.STAGE_OUTPUT）里的 schema_version / params；
    - runner 在阶段完成时写入 stage_meta.json 的参数快照（覆盖 audio.wav、共用的 cues.json）。
    """
    statuses = {n: r["status"] for n, r in _stage_rows(video_id).items()}
    try:
        path = artifacts.video_dir(video_id) / STAGE_META_FILE
        recorded = artifacts.read_json(path) if path.exists() else {}
    except Exception:
        recorded = {}

    changed: list[str] = []
    for name in STAGES:
        if name == AWAIT or statuses.get(name) != "done":
            continue
        metas = [recorded.get(name)]
        output = _owned_json_output(name)
        if output:
            try:
                metas.append(artifacts.read_artifact_meta(video_id, output))
            except Exception:
                pass
        expected = _norm(config.settings.stage_params(name))
        if any(m is not None and (m.get("schema_version") != SCHEMA_VERSION
                                  or _norm(m.get("params")) != expected) for m in metas):
            changed.append(name)
    # 本身不一致的阶段 + 已跑过（非 pending）的下游
    stale = [n for n in downstream(changed)
             if n in changed or statuses.get(n, "pending") != "pending"]
    return stale
