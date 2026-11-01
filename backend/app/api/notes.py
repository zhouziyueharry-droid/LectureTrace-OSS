"""不启动模型的笔记接口。"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .videos import get_video
from ..qa import notes

router = APIRouter(prefix='/api/videos/{video_id}/notes', tags=['notes'])


class CreateBody(BaseModel):
    name: str | None = None
    title: str | None = None
    text: str = ''


class RevisionBody(BaseModel):
    base_revision: str


class SaveBody(RevisionBody):
    text: str


class RenameBody(RevisionBody):
    new_name: str


class RestoreBody(RevisionBody):
    version_id: str


class ResolveBody(RevisionBody):
    proposal_id: str
    text: str = ''
    accepted: int
    total: int


def _call(video_id, function, *args, **kwargs):
    try:
        with notes.lock(video_id):
            get_video(video_id)
            return function(video_id, *args, **kwargs)
    except notes.NoteError as e:
        raise HTTPException(e.status_code, str(e)) from e


@router.get('')
def listing(video_id: str):
    _call(video_id, notes.import_all_legacy)
    return _call(video_id, notes.listing)


@router.post('')
def create(video_id: str, body: CreateBody):
    return _call(video_id, notes.create, name=body.name, title=body.title, text=body.text)


@router.get('/trash')
def trash(video_id: str):
    return _call(video_id, notes.list_trash)


@router.post('/trash/{trash_id}/restore')
def restore_trash(video_id: str, trash_id: str):
    return _call(video_id, notes.restore_trash, trash_id)


@router.get('/{name}')
def read(video_id: str, name: str):
    return _call(video_id, notes.read, name)


@router.put('/{name}')
def save(video_id: str, name: str, body: SaveBody):
    # 作者由服务端决定：页面里的保存一律记为用户，Agent 只能通过工具写入。
    return _call(video_id, notes.save, name, body.text, body.base_revision)


@router.delete('/{name}')
def delete(video_id: str, name: str):
    return _call(video_id, notes.delete, name)


@router.post('/{name}/undo')
def undo(video_id: str, name: str, body: RevisionBody):
    return _call(video_id, notes.undo, name, body.base_revision)


@router.post('/{name}/rename')
def rename(video_id: str, name: str, body: RenameBody):
    return _call(video_id, notes.rename, name, body.new_name, body.base_revision)


@router.post('/{name}/seen')
def seen(video_id: str, name: str):
    return _call(video_id, notes.mark_seen, name)


@router.get('/{name}/versions')
def versions(video_id: str, name: str):
    return _call(video_id, notes.versions, name)


@router.get('/{name}/versions/{version_id}')
def read_version(video_id: str, name: str, version_id: str):
    return _call(video_id, notes.read_version, name, version_id)


@router.post('/{name}/restore')
def restore(video_id: str, name: str, body: RestoreBody):
    return _call(video_id, notes.restore, name, body.version_id, body.base_revision)


@router.get('/{name}/proposal')
def proposal(video_id: str, name: str):
    return _call(video_id, notes.proposal, name)


@router.post('/{name}/proposal/resolve')
def resolve_proposal(video_id: str, name: str, body: ResolveBody):
    return _call(video_id, notes.resolve_proposal, name, body.proposal_id, body.text,
                 body.base_revision, body.accepted, body.total)


@router.delete('/{name}/proposal')
def discard_proposal(video_id: str, name: str):
    return _call(video_id, notes.discard_proposal, name)
