"""Raw streaming uploads: no multipart buffering and no model calls."""
import shutil
import uuid

import anyio
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from .notes import _call
from .videos import get_video
from .. import pdfs
from ..config import settings

router = APIRouter(prefix='/api/videos/{video_id}/pdfs', tags=['pdfs'])


@router.get('')
def listing(video_id: str):
    return _call(video_id, pdfs.listing)


@router.get('/trash')
def trash(video_id: str):
    return _call(video_id, pdfs.list_trash)


@router.post('')
async def upload(video_id: str, name: str, request: Request):
    get_video(video_id)
    _call(video_id, lambda _: pdfs.valid_name(name))
    length = request.headers.get('content-length')
    if length and (not length.isdecimal() or int(length) > settings.pdfs.max_bytes):
        raise HTTPException(413, 'PDF 超过大小上限（32 MiB）')
    directory = pdfs.root(video_id) / ('.upload-' + uuid.uuid4().hex)
    directory.mkdir(parents=True)
    try:
        total = 0
        async with await anyio.open_file(directory / 'source.pdf', 'wb') as handle:
            async for chunk in request.stream():
                total += len(chunk)
                if total > settings.pdfs.max_bytes:
                    raise HTTPException(413, 'PDF 超过大小上限（32 MiB）')
                await handle.write(chunk)
        return await anyio.to_thread.run_sync(_call, video_id, pdfs.publish, directory, name)
    finally:
        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync(lambda: shutil.rmtree(directory) if directory.exists() else None)


@router.get('/{file_id}/file')
def original(video_id: str, file_id: str, revision: str | None = None, download: bool = False):
    meta = _call(video_id, pdfs.metadata, file_id, revision)
    return FileResponse(pdfs.safe_file(pdfs.folder(video_id, file_id), 'source.pdf'),
                        media_type='application/pdf', filename=meta['name'],
                        content_disposition_type='attachment' if download else 'inline',
                        headers={'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-store',
                                 'Content-Security-Policy': "default-src 'none'; sandbox"})


@router.delete('/{file_id}')
def delete(video_id: str, file_id: str):
    return _call(video_id, pdfs.delete, file_id)


@router.post('/trash/{file_id}/restore')
def restore(video_id: str, file_id: str):
    return _call(video_id, pdfs.restore, file_id)
