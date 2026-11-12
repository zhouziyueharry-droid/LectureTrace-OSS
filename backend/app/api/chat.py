"""问答接口：读取历史 / 提问（SSE 流式）/ 管理对话。"""
from __future__ import annotations

import importlib
import asyncio
import json
import queue
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from .videos import get_video

router = APIRouter(prefix="/api/videos", tags=["chat"])


class NoteTarget(BaseModel):
    name: str
    revision: str


class PdfTarget(BaseModel):
    id: str
    revision: str


class AskBody(BaseModel):
    question: str
    node_id: str | None = None
    allow_outline_edit: bool = False
    note_target: NoteTarget | None = None
    pdf_target: PdfTarget | None = None
    learning_action: Literal['clarify', 'formula', 'example', 'slides', 'course_context', 'prerequisite', 'analogy',
                             'counterexample', 'comparison', 'application', 'practice', 'custom', 'plain_language'] | None = None
    learning_prompt: str | None = Field(default=None, max_length=180)
    learning_scope: Literal['course', 'supplement'] | None = None
    learning_parent_question: str | None = Field(default=None, max_length=4000)
    run_id: str | None = Field(default=None, max_length=80, pattern=r'^[A-Za-z0-9-]+$')


class RenameBody(BaseModel):
    title: str


class ModelSelection(BaseModel):
    model: str = Field(min_length=1, max_length=128)
    effort: str = Field(min_length=1, max_length=32)


class InteractionResponse(BaseModel):
    request_id: str
    decision: str = 'decline'
    answers: dict[str, str] = Field(default_factory=dict)


@router.post('/{video_id}/chat/respond')
def chat_respond(video_id: str, body: InteractionResponse):
    get_video(video_id)
    from ..qa import codex_chat
    try:
        codex_chat.respond_interaction(video_id, body.request_id, body.decision, body.answers)
        return {'ok': True}
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@router.get('/{video_id}/chat/model')
def chat_model(video_id: str):
    get_video(video_id)
    from ..qa import codex_chat
    return codex_chat.model_selection(video_id)


@router.put('/{video_id}/chat/model')
def chat_select_model(video_id: str, body: ModelSelection):
    get_video(video_id)
    from ..qa import codex_chat, conversations
    from ..codex.runtime import CodexError
    try:
        return codex_chat.select_model(video_id, body.model, body.effort)
    except conversations.BusyError as error:
        raise HTTPException(409, str(error)) from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except CodexError as error:
        raise HTTPException(503, str(error)) from error


def _qa():
    try:
        return importlib.import_module("app.qa.chat")
    except ImportError:
        raise HTTPException(503, "问答模块尚未就绪")


@router.get("/{video_id}/chat")
def chat_history(video_id: str):
    get_video(video_id)
    return _qa().history(video_id)


@router.delete("/{video_id}/chat")
def chat_clear(video_id: str):
    get_video(video_id)
    from ..qa import conversations
    try:
        return conversations.delete_current(video_id)
    except conversations.BusyError as e:
        raise HTTPException(409, str(e)) from e


@router.get('/{video_id}/chat/conversations')
def chat_conversations(video_id: str):
    get_video(video_id)
    from ..qa import conversations
    return conversations.listing(video_id)


@router.post('/{video_id}/chat/conversations')
def chat_create(video_id: str):
    get_video(video_id)
    from ..qa import conversations
    try:
        return conversations.create(video_id)
    except conversations.BusyError as e:
        raise HTTPException(409, str(e)) from e


@router.put('/{video_id}/chat/conversations/{conversation_id}')
def chat_select(video_id: str, conversation_id: str):
    get_video(video_id)
    from ..qa import conversations
    try:
        return conversations.select(video_id, conversation_id)
    except conversations.BusyError as e:
        raise HTTPException(409, str(e)) from e
    except conversations.MissingError as e:
        raise HTTPException(404, str(e)) from e


@router.patch('/{video_id}/chat/conversations/{conversation_id}')
def chat_rename(video_id: str, conversation_id: str, body: RenameBody):
    get_video(video_id)
    from ..qa import conversations
    try:
        return conversations.rename(video_id, conversation_id, body.title)
    except conversations.InvalidTitleError as e:
        raise HTTPException(400, str(e)) from e
    except conversations.BusyError as e:
        raise HTTPException(409, str(e)) from e
    except conversations.MissingError as e:
        raise HTTPException(404, str(e)) from e


@router.get('/{video_id}/chat/context')
def chat_context(video_id: str):
    get_video(video_id)
    from ..qa import codex_chat
    return codex_chat.context_snapshot(video_id)


@router.post("/{video_id}/chat")
async def chat_ask(video_id: str, body: AskBody):
    get_video(video_id)
    question = body.question.strip()
    if not question:
        raise HTTPException(400, "问题不能为空")
    qa = _qa()
    from ..qa import chat_runs, conversations
    options = body.model_dump(exclude={'question', 'run_id'}, exclude_none=True, exclude_defaults=True)
    try:
        run = chat_runs.start(video_id, question, options, body.run_id, qa.ask)
    except conversations.BusyError as error:
        raise HTTPException(409, str(error)) from error
    events = run.events

    async def gen():
        try:
            while True:
                try:
                    item = await asyncio.to_thread(events.get, True, 0.5)
                except queue.Empty:
                    continue
                yield {"data": json.dumps(item, ensure_ascii=False)}
                if item.get('type') in ('done', 'error'):
                    break
        finally:
            run.detach()

    return EventSourceResponse(gen(), ping=15)


@router.get('/{video_id}/chat/run')
def chat_run(video_id: str, conversation_id: str | None = None):
    get_video(video_id)
    from ..qa import chat_runs
    return chat_runs.snapshot(video_id, conversation_id)


@router.post('/{video_id}/chat/stop')
def chat_stop(video_id: str, run_id: str | None = None):
    get_video(video_id)
    from ..qa import chat_runs
    chat_runs.stop(video_id, run_id)
    return {'ok': True}
