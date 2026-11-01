"""Explicit user commit and undo of validated outline previews."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .videos import get_video
from ..qa import outline_edits

router = APIRouter(prefix="/api/videos", tags=["outline"])


class CommitBody(BaseModel):
    proposal_id: str


class UndoBody(BaseModel):
    base_revision: str


@router.post("/{video_id}/outline/commit")
def commit(video_id: str, body: CommitBody):
    get_video(video_id)
    try:
        return outline_edits.commit(video_id, body.proposal_id)
    except FileNotFoundError:
        raise HTTPException(404, "大纲尚未生成") from None
    except outline_edits.OutlineEditError as error:
        raise HTTPException(error.status_code, str(error)) from error


@router.post("/{video_id}/outline/undo")
def undo(video_id: str, body: UndoBody):
    get_video(video_id)
    try:
        return outline_edits.undo(video_id, body.base_revision)
    except FileNotFoundError:
        raise HTTPException(404, "大纲尚未生成") from None
    except outline_edits.OutlineEditError as error:
        raise HTTPException(error.status_code, str(error)) from error
