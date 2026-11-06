"""Chat adapter between the tutoring graph and LectureTrace's SSE contract."""
from __future__ import annotations

import logging
from typing import Iterator

from ... import config
from .. import citations, context
from . import llm, settings as agent_settings
from .graph import TUTOR_GRAPH

log = logging.getLogger(__name__)

PROGRESS = {
    "analyze": "判断问题类型并安排讲解方式",
    "retrieve": "检索课程字幕、幻灯片和证据",
    "assess_evidence": "检查课程证据是否覆盖问题",
    "plan_lesson": "组织教学步骤",
    "draft_answer": "按步骤起草解释",
    "review_teaching": "检查解释连贯性和引用",
    "revise_once": "修正讲解中的问题",
}


def ask(video_id: str, question: str, *, node_id: str | None = None, allow_outline_edit: bool = False,
        note_target: dict | None = None, pdf_target: dict | None = None) -> Iterator[dict]:
    from .. import chat as chat_service, codex_chat, conversations

    question = (question or "").strip()
    if not question:
        yield {"type": "error", "error": "问题不能为空"}
        return
    if allow_outline_edit or note_target is not None or pdf_target is not None:
        yield {"type": "error", "error": "教学 Agent 目前只处理课程问答；页面/PDF 附件和大纲修改请切换到 Codex Agent"}
        return
    lock = codex_chat.conversation_lock(video_id)
    if not lock.acquire(blocking=False):
        yield {"type": "error", "error": "这节课正在回答，请等待或停止当前回答"}
        return
    try:
        yield from _ask_locked(video_id, question, node_id=node_id)
    finally:
        lock.release()


def _ask_locked(video_id: str, question: str, *, node_id: str | None) -> Iterator[dict]:
    from .. import chat as chat_service, conversations

    conversation_id = conversations.current(video_id)
    try:
        ctx = context.build_context(video_id)
    except context.ContextError as error:
        yield {"type": "error", "error": str(error)}
        return

    history = chat_service.usable_turns(chat_service.history(video_id))
    history_ids = {
        cite["id"] for message in history if message["role"] == "assistant"
        for cite in message.get("citations") or [] if cite.get("id") in ctx.index
    }
    chat_service._insert(video_id, "user", question, None, version=ctx.version, conversation_id=conversation_id)
    state = {
        "video_id": video_id,
        "question": question,
        "node_id": node_id,
        "context": ctx,
        "history": history,
        "history_citation_ids": sorted(history_ids),
    }
    current = dict(state)
    yield {"type": "phase", "phase": "locate"}
    try:
        for update in TUTOR_GRAPH.stream(state, stream_mode="updates", config={"recursion_limit": 20}):
            for node, values in update.items():
                current.update(values)
                yield {"type": "progress", "text": PROGRESS.get(node, "正在整理回答")}
    except llm.LLMError as error:
        yield {"type": "error", "error": str(error)}
        return
    except Exception as error:  # noqa: BLE001
        log.exception("教学 Agent 运行失败（%s）", video_id)
        yield {"type": "error", "error": f"教学 Agent 暂时无法完成回答：{error}"}
        return

    exp = current.get("expansion") or context.Expansion()
    yield {"type": "phase", "phase": "answer", "expanded": {"t": len(exp.t_ids), "images": len(exp.images)}}
    answer = citations.normalize(current.get("answer", "").strip())
    if not answer:
        answer = "课程资料中没有找到足够内容来解释这个问题。你可以指定一个术语、公式或视频时间点，我会从相关片段开始讲解。"
    allowed = set(ctx.prefix_ids) | set(exp.ids) | history_ids
    cites, invalid = citations.resolve(answer, ctx.index, allowed)
    if invalid:
        prompt = chat_service.build_messages(
            history,
            context.answer_content(
                f"学生问题：{question}\n教学计划：\n{current.get('lesson_plan', '')}", exp,
            ),
        )
        fix_prompt = chat_service._fix_prompt(invalid, allowed, exp.ids, history_ids)
        try:
            fixed_raw = llm.complete(
                messages=prompt + [{"role": "assistant", "content": answer},
                                   {"role": "user", "content": fix_prompt}],
                system=ctx.system, max_tokens=agent_settings.get()["max_output_tokens"],
                video_id=video_id, stage="citation_repair", effort=agent_settings.effort(),
            )
            fixed = citations.normalize(fixed_raw.strip()) or None
        except llm.LLMError:
            log.exception("教学 Agent 引用修正请求失败")
            fixed = None
        if fixed is not None:
            answer = fixed
            cites, invalid = citations.resolve(answer, ctx.index, allowed)
            yield {"type": "replace", "text": answer}
    if invalid:
        chat_service._insert(video_id, "assistant", answer, cites, version=ctx.version,
                             status="failed", conversation_id=conversation_id)
        yield {"type": "error", "error": chat_service.CITATION_ERROR}
        return
    yield {"type": "delta", "text": answer}
    yield {"type": "done", "message": chat_service._insert(
        video_id, "assistant", answer, cites, version=ctx.version, conversation_id=conversation_id,
    )}

