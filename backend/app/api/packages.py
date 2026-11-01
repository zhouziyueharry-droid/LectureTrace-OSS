"""Course archive transfer. Raw upload keeps large MP4s out of application memory."""
from pathlib import Path
import json
import logging
import sqlite3
import threading
import time
import uuid

import anyio
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from sse_starlette.sse import EventSourceResponse

from .. import course_packages as packages
from .. import events
from ..config import settings

router = APIRouter(tags=['packages'])


def _call(fn, *args):
    try:
        return fn(*args)
    except packages.PackageError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None
    except (OSError, ValueError):
        raise HTTPException(400, '课程包读写失败，请检查文件完整性和剩余磁盘空间') from None
    except sqlite3.Error:
        raise HTTPException(500, '课程包数据库写入失败，导入未完成，请重试') from None


@router.post('/api/videos/{video_id}/package')
def export_package(video_id: str, request: Request):
    if 'text/event-stream' in request.headers.get('accept', ''):
        return _stream_export(packages.export, video_id)
    return _call(packages.export, video_id)


@router.post('/api/courses/{course_id}/package')
def export_course_package(course_id: str, request: Request):
    if 'text/event-stream' in request.headers.get('accept', ''):
        return _stream_export(packages.export_course, course_id)
    return _call(packages.export_course, course_id)


def _stream_export(fn, identity):
    async def generate():
        channel = f'package:{uuid.uuid4().hex}'
        last_sent, last_step = 0, None

        def progress(value):
            nonlocal last_sent, last_step
            step = (value['phase'], value['lecture_index'])
            now = time.monotonic()
            if step != last_step or now - last_sent >= 0.2:
                events.publish(channel, {'type': 'progress', **value})
                last_sent, last_step = now, step

        def run():
            try:
                result = _call(fn, identity, progress)
                events.publish(channel, {'type': 'done', 'result': result})
            except HTTPException as exc:
                events.publish(channel, {'type': 'error', 'detail': exc.detail, 'status': exc.status_code})
            except Exception:
                logging.getLogger(__name__).exception('课程包导出失败')
                events.publish(channel, {'type': 'error', 'detail': '课程包导出失败，请重试', 'status': 500})

        async with events.subscribe(channel) as subscription:
            yield {'data': json.dumps({'type': 'progress', 'phase': 'waiting', 'written_bytes': 0,
                                      'total_bytes': 0, 'lecture_index': 0, 'lecture_count': 0, 'lecture_title': ''})}
            threading.Thread(target=run, daemon=True, name='package-export').start()
            async for event in subscription:
                yield {'data': json.dumps(event, ensure_ascii=False)}
                if event['type'] in ('done', 'error'):
                    break

    return EventSourceResponse(generate(), ping=15)


def _download(token: str) -> Path:
    path = packages.stage_path(token) / 'export.ltcourse'
    if not path.is_file():
        raise packages.PackageError('下载不存在或已过期', 404)
    return path


@router.get('/api/packages/{token}/download')
def download_package(token: str):
    path = _call(_download, token)
    # Keep until TTL/explicit cleanup: browsers can use ranges and retry downloads.
    return FileResponse(path, filename='LectureTrace.ltcourse', media_type='application/zip')


@router.post('/api/packages/preview')
async def preview_package(request: Request):
    length = request.headers.get('content-length')
    if length and (not length.isdecimal() or int(length) > settings.packages.max_archive_bytes):
        raise HTTPException(413, '课程包超过大小上限（32 GiB）')
    token, directory = await anyio.to_thread.run_sync(packages.new_stage)
    try:
        total = 0
        async with await anyio.open_file(directory / 'upload.zip', 'wb') as f:
            async for chunk in request.stream():
                total += len(chunk)
                if total > settings.packages.max_archive_bytes:
                    raise HTTPException(413, '课程包超过大小上限（32 GiB）')
                await f.write(chunk)
        return await anyio.to_thread.run_sync(_call, packages.inspect_archive, token)
    except BaseException:
        # Shield cleanup from client cancellation; no partial upload enters videos/.
        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync(packages.discard, token)
        raise


@router.post('/api/packages/{token}/import')
def import_package(token: str):
    return _call(packages.commit, token)


@router.delete('/api/packages/{token}')
def delete_package(token: str):
    _call(packages.discard, token)
    return {'ok': True}
