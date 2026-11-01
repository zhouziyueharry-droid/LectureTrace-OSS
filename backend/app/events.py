"""进程内发布/订阅。

工作线程调用 publish；FastAPI 的 SSE 处理函数用 `async with subscribe(video_id) as sub` 接收。
投递到 asyncio 队列时用 loop.call_soon_threadsafe，保证跨线程安全。
测试或同步代码可用 subscribe_sync 拿到一个 queue.Queue。
"""
from __future__ import annotations

import asyncio
import queue
import threading
from contextlib import contextmanager
from typing import Any, Callable, Iterator

# video_id -> {订阅 id: 投递函数}
_subs: dict[str, dict[int, Callable[[dict], None]]] = {}
_lock = threading.Lock()
_next_id = 0


def _add(video_id: str, deliver: Callable[[dict], None]) -> int:
    global _next_id
    with _lock:
        _next_id += 1
        _subs.setdefault(video_id, {})[_next_id] = deliver
        return _next_id


def _remove(video_id: str, sub_id: int) -> None:
    with _lock:
        subs = _subs.get(video_id)
        if subs is not None:
            subs.pop(sub_id, None)
            if not subs:
                _subs.pop(video_id, None)


def publish(video_id: str, event: dict[str, Any]) -> None:
    """可在任意线程调用；投递失败（事件循环已关闭等）静默忽略。"""
    with _lock:
        delivers = list(_subs.get(video_id, {}).values())
    for deliver in delivers:
        try:
            deliver(event)
        except RuntimeError:
            pass


class Subscription:
    """异步订阅。创建时立即登记（之后发布的事件都不会丢），用完调用 close 或用 async with。"""

    def __init__(self, video_id: str) -> None:
        loop = asyncio.get_running_loop()
        self.video_id = video_id
        self._q: asyncio.Queue[dict] = asyncio.Queue()
        self._id = _add(video_id, lambda ev: loop.call_soon_threadsafe(self._q.put_nowait, ev))

    def __aiter__(self) -> "Subscription":
        return self

    async def __anext__(self) -> dict:
        return await self._q.get()

    def close(self) -> None:
        _remove(self.video_id, self._id)

    async def __aenter__(self) -> "Subscription":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.close()


def subscribe(video_id: str) -> Subscription:
    """在事件循环内调用：`async with subscribe(vid) as sub: async for ev in sub: ...`"""
    return Subscription(video_id)


@contextmanager
def subscribe_sync(video_id: str) -> Iterator[queue.Queue]:
    """同步订阅：with subscribe_sync(vid) as q: q.get(timeout=...)"""
    q: queue.Queue[dict] = queue.Queue()
    sub_id = _add(video_id, q.put)
    try:
        yield q
    finally:
        _remove(video_id, sub_id)


def subscriber_count(video_id: str) -> int:
    with _lock:
        return len(_subs.get(video_id, {}))
