"""Chat adapter between the tutoring graph and LectureTrace's SSE contract."""
from __future__ import annotations

import logging
from typing import Iterator

from ... import config
from .. import citations, context
from . import llm, settings as agent_settings
from .graph import TUTOR_GRAPH, is_course_grounded_open_task

log = logging.getLogger(__name__)
LEARNING_ACTIONS = {"clarify", "formula", "example", "slides", "course_context", "prerequisite", "analogy",
                    "counterexample", "comparison", "application", "practice", "custom"}

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
        note_target: dict | None = None, pdf_target: dict | None = None,
        learning_action: str | None = None, learning_prompt: str | None = None,
        learning_scope: str | None = None, learning_parent_question: str | None = None) -> Iterator[dict]:
    from .. import chat as chat_service, codex_chat, conversations

    question = (question or "").strip()
    if not question:
        yield {"type": "error", "error": "问题不能为空"}
        return
    if allow_outline_edit or note_target is not None or pdf_target is not None:
        yield {"type": "error", "error": "步进式问答 Agent 目前只处理课程问答；页面/PDF 附件和大纲修改请切换到 Codex Agent"}
        return
    if learning_action is not None and learning_action not in LEARNING_ACTIONS:
        yield {"type": "error", "error": "未知的知识拓展选项，请刷新页面后重试"}
        return
    if learning_action and not (learning_parent_question or "").strip():
        yield {"type": "error", "error": "找不到要继续拓展的原问题，请重新提问"}
        return
    if learning_action and learning_scope not in ("course", "supplement"):
        yield {"type": "error", "error": "知识拓展缺少来源范围，请刷新页面后重试"}
        return
    lock = codex_chat.conversation_lock(video_id)
    if not lock.acquire(blocking=False):
        yield {"type": "error", "error": "这节课正在回答，请等待或停止当前回答"}
        return
    try:
        yield from _ask_locked(video_id, question, node_id=node_id, learning_action=learning_action,
                               learning_prompt=learning_prompt, learning_scope=learning_scope,
                               learning_parent_question=learning_parent_question)
    finally:
        lock.release()


def _ask_locked(video_id: str, question: str, *, node_id: str | None, learning_action: str | None,
                learning_prompt: str | None, learning_scope: str | None,
                learning_parent_question: str | None) -> Iterator[dict]:
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
        "learning_action": learning_action,
        "learning_prompt": (learning_prompt or "").strip() or None,
        "learning_scope": learning_scope,
        "learning_parent_question": (learning_parent_question or "").strip() or None,
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
        log.exception("步进式问答 Agent 运行失败（%s）", video_id)
        yield {"type": "error", "error": f"步进式问答 Agent 暂时无法完成回答：{error}"}
        return

    exp = current.get("expansion") or context.Expansion()
    yield {"type": "phase", "phase": "answer", "expanded": {"t": len(exp.t_ids), "images": len(exp.images)}}
    answer = citations.normalize(current.get("answer", "").strip())
    if learning_scope == "supplement" and not is_course_grounded_open_task(current):
        marker = "【课外补充】" if agent_settings.get()["output_language"] == "zh-CN" else "[Supplementary · outside course]"
        if not answer.startswith(marker):
            answer = f"{marker} {answer}"
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
        fix_prompt += "\n只输出给学生看的完整答案；不要附加证据检索、引用修订或作答过程说明。"
        try:
            fixed_raw = llm.complete(
                messages=prompt + [{"role": "assistant", "content": answer},
                                   {"role": "user", "content": fix_prompt}],
                system=ctx.system, max_tokens=agent_settings.get()["max_output_tokens"],
                video_id=video_id, stage="citation_repair", effort=agent_settings.effort(),
            )
            fixed = citations.normalize(fixed_raw.strip()) or None
        except llm.LLMError:
            log.exception("步进式问答 Agent 引用修正请求失败")
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
    intent = current.get("intent", "concept")
    root_question = (learning_parent_question or question).strip()
    allowed = set(ctx.prefix_ids) | set(exp.ids) | history_ids
    options = _validated_learning_options(current.get("learning_options") or [], root_question, allowed)
    if allowed and not any(option["id"] == "course_context" for option in options):
        options.append({
            "id": "course_context",
            "label": "放回课程整体看",
            "prompt": "从课程总体看，当前回答服务了课程中的什么概念？课程为什么要讲这个概念？请结合课程前后内容简要说明。",
            "scope": "course",
            "evidence_ids": sorted(allowed)[:3],
            "parent_question": root_question,
        })
    yield {"type": "done", "message": chat_service._insert(
        video_id, "assistant", answer, cites, version=ctx.version, conversation_id=conversation_id,
        learning_options=options,
    )}


def _validated_learning_options(raw_options: list[dict], question: str, allowed: set[str]) -> list[dict]:
    """Keep generated suggestions bounded and require evidence IDs for course-labeled options."""
    options = []
    for item in raw_options[:3]:
        if not isinstance(item, dict):
            continue
        action = item.get("id")
        label = str(item.get("label") or "").strip()[:36]
        prompt = str(item.get("prompt") or "").strip()[:180]
        scope = item.get("scope")
        if action not in LEARNING_ACTIONS or not label or not prompt or scope not in ("course", "supplement"):
            continue
        raw_evidence_ids = item.get("evidence_ids")
        if not isinstance(raw_evidence_ids, list):
            raw_evidence_ids = []
        evidence_ids = list(dict.fromkeys(i for i in raw_evidence_ids
                                          if isinstance(i, str) and i in allowed))
        # A course-labeled option without a verified course reference would mislead the learner.
        if scope == "course" and not evidence_ids:
            scope = "supplement"
        options.append({"id": action, "label": label, "prompt": prompt, "scope": scope,
                        "evidence_ids": evidence_ids if scope == "course" else [],
                        "parent_question": question})
    return options

