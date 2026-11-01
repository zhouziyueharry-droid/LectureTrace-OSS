"""问答会话（交接 0001 §10）：历史存 chat_messages。

每轮两步，共用同一个系统前缀（context.build_context）与同一个 prompt_cache_key（stage="qa"、cache_system=True）：
1. 定位（非流式）：模型只输出 {"expand": [...], "images": [...]}。不加 response_format——返回格式的 schema
   属于缓存前缀，两步格式不同会互相无法命中缓存。本地宽松解析，失败按「无需展开」。
2. 回答（流式）：问题后附【展开的证据】（T 原文与截图）。
结束后校验引用：只允许 短索引中的 ID ∪ 本轮展开的 ID ∪ 历史有效回答引用过且仍存在的 ID。
有无效 ID 时自动修正一次（非流式），通过 {"type":"replace"} 替换；仍无效则 {"type":"error"}，回答以 status="failed" 保存。

SSE 事件（前端契约）：
  {"type":"phase","phase":"locate"}
  {"type":"phase","phase":"answer","expanded":{"t":N,"images":M}}
  {"type":"delta","text":...}
  {"type":"replace","text":...}
  {"type":"error","error":"中文原因"}
  {"type":"done","message":{"id","role","content","citations":[{"id","start"}],"status","stale","created_at"}}
"""
from __future__ import annotations

import json
import logging
from typing import Iterator

from .. import config, db
from ..llm import client as llm, settings as llm_settings
from . import citations, context
from .context import ContextError, QaContext

log = logging.getLogger(__name__)

STAGE = "qa"               # 两步与修正都用同一个 stage：模型、档位、prompt_cache_key 都相同
MAX_LOCATE_TOKENS = 16000  # 含推理 token；只是上限，按实际用量计费
MAX_ANSWER_TOKENS = 32000
CITATION_ERROR = "回答引用校验失败，请重试"

FIX_TEMPLATE = """你的回答引用了不存在或本轮不允许引用的证据 ID：{invalid}。
允许引用的 ID：短索引中出现过的 ID（页 S012、出现 S012.1、无幻灯片时段 N001、讲解片段 T0031）{extra}。{hints}
请只修改引用：把无效 ID 换成确实支撑该论断的有效 ID，找不到就删掉这个引用；其余内容保持不变。
直接输出修改后的完整回答，不要解释，不要加前言。"""


# ---------------------------------------------------------------- 历史

def _row_to_msg(r: dict, current_version: str | None) -> dict:
    if str(r.get('evidence_version') or '').startswith('sha256:'):
        from .outline_edits import evidence_version
        try:
            current_version = 'sha256:' + evidence_version(r['video_id'])
        except (FileNotFoundError, ValueError):
            current_version = None
    stale = r.get("evidence_version") != current_version
    status = r.get("status") or "ok"
    cites = json.loads(r["citations_json"]) if r.get("citations_json") and not stale else []
    from .. import pdfs
    pdf_valid, _ = pdfs.resolve(r['content'], r['video_id'], {c['id'] for c in cites})
    cites = [c for c in cites if not c['id'].startswith('PDF:')] + pdf_valid
    msg = {"id": r["id"], "role": r["role"], "content": r["content"], "citations": cites,
           "status": status, "stale": stale, "created_at": r["created_at"]}
    if status == "failed":
        msg["error"] = CITATION_ERROR
    return msg


def history(video_id: str) -> list[dict]:
    """全部消息。版本与当前 evidence 不一致的（含旧版无版本的消息）citations 置空、stale=True。"""
    current = context.current_version(video_id)
    from . import conversations
    rows = db.fetch_all("SELECT * FROM chat_messages WHERE video_id = ? AND conversation_id = ? ORDER BY id",
                        (video_id, conversations.current(video_id)))
    return [_row_to_msg(r, current) for r in rows]


def clear(video_id: str) -> None:
    from . import codex_chat
    codex_chat.reset(video_id)


def _insert(video_id: str, role: str, content: str, cites: list[dict] | None, *,
            version: str | None, status: str = "ok", conversation_id: str | None = None) -> dict:
    from . import conversations
    conversation_id = conversation_id or conversations.current(video_id)
    created = db.now_iso()
    cj = json.dumps(cites, ensure_ascii=False) if cites is not None else None
    mid = db.execute("INSERT INTO chat_messages (video_id, conversation_id, role, content, citations_json, created_at, status,"
                     " evidence_version) VALUES (?,?,?,?,?,?,?,?)",
                     (video_id, conversation_id, role, content, cj, created, status, version))
    db.execute('UPDATE chat_conversations SET updated_at=? WHERE video_id=? AND id=?',
               (created, video_id, conversation_id))
    msg = {"id": mid, "role": role, "content": content, "citations": cites or [], "status": status,
           "stale": False, "created_at": created}
    if status == "failed":
        msg["error"] = CITATION_ERROR
    return msg


def usable_turns(past: list[dict]) -> list[dict]:
    """下一轮上下文里带入的历史：只取「问题 + 正常完成的回答」成对的轮次。
    失败的回答、没有回答的问题（上次出错）、旧版本证据下的消息都不带入。只带文字，不重发上一轮展开的证据。"""
    out: list[dict] = []
    pending: dict | None = None
    for m in past:
        if m["role"] == "user":
            pending = m
            continue
        ok = m.get("status", "ok") == "ok" and not m.get("stale")
        if pending is not None and ok and not pending.get("stale"):
            out += [pending, m]
        pending = None
    return out


def build_messages(turns: list[dict], final: str | list[dict]) -> list[dict]:
    """历史轮次 + 本轮最后一条用户消息。历史末尾加缓存断点（Anthropic 分支用；OpenAI 分支只在系统消息设断点），
    定位、回答、修正三次请求的历史部分相同。"""
    msgs: list[dict] = [{"role": m["role"], "content": m["content"]} for m in turns]
    if msgs:
        last = msgs[-1]
        last["content"] = [{"type": "text", "text": last["content"], "cache_control": {"type": "ephemeral"}}]
    msgs.append({"role": "user", "content": final})
    return msgs


# ---------------------------------------------------------------- 引用修正

def _hints(invalid: list[str], allowed: set[str]) -> str:
    """对 S012.3 这类「页存在、出现编号不对」的无效 ID，列出同一页可用的 ID，方便模型改对。"""
    out = []
    for i in invalid:
        page = i.split(".")[0]
        if page == i or not page.startswith("S"):
            continue
        cands = sorted(a for a in allowed if a == page or a.startswith(page + "."))
        if cands:
            out.append(f"{i} 可改用 {'、'.join(cands[:10])}")
    return ("\n" + "；".join(out) + "。") if out else ""


def _fix_prompt(invalid: list[str], allowed: set[str], expanded: list[str], hist_ids: set[str]) -> str:
    extra = ""
    if expanded:
        extra += "，本轮展开的 ID：" + "、".join(expanded)
    if hist_ids:
        extra += "，之前回答中引用过的 ID：" + "、".join(sorted(hist_ids))
    return FIX_TEMPLATE.format(invalid="、".join(invalid), extra=extra, hints=_hints(invalid, allowed))


def _fix_citations(ctx: QaContext, messages: list[dict], answer: str, invalid: list[str], allowed: set[str],
                   expanded: list[str], hist_ids: set[str], effort: str) -> str | None:
    """非流式请求一次，要求只改引用重写回答。失败返回 None。"""
    convo = messages + [{"role": "assistant", "content": answer},
                        {"role": "user", "content": _fix_prompt(invalid, allowed, expanded, hist_ids)}]
    try:
        text = llm.complete(messages=convo, system=ctx.system, max_tokens=MAX_ANSWER_TOKENS,
                            video_id=ctx.video_id, stage=STAGE, cache_system=True, effort=effort)
    except Exception:  # noqa: BLE001  修正失败按校验失败处理
        log.exception("引用修正请求失败")
        return None
    return citations.normalize(text.strip()) or None


# ---------------------------------------------------------------- 一轮问答

def ask(video_id: str, question: str, *, node_id: str | None = None,
        allow_outline_edit: bool = False, note_target: dict | None = None, pdf_target: dict | None = None) -> Iterator[dict]:
    from ..codex import settings as codex_settings
    from . import codex_chat
    if codex_settings.get()['provider'] == 'codex':
        options = {'pdf_target': pdf_target} if pdf_target is not None else {}
        yield from codex_chat.ask(video_id, question, node_id=node_id, allow_outline_edit=allow_outline_edit, note_target=note_target, **options)
        return
    if note_target is not None or pdf_target is not None:
        yield {'type': 'error', 'error': '编辑工作区笔记需要在设置中切换到 Codex Agent'}
        return
    lock = codex_chat.conversation_lock(video_id)
    if not lock.acquire(blocking=False):
        yield {'type': 'error', 'error': '这节课正在回答，请等待或停止当前回答'}
        return
    try:
        yield from _ask_api(video_id, question, allow_outline_edit=allow_outline_edit)
    finally:
        lock.release()


def _ask_api(video_id: str, question: str, *, allow_outline_edit: bool = False) -> Iterator[dict]:
    from . import conversations
    conversation_id = conversations.current(video_id)
    if allow_outline_edit:
        yield {'type': 'error', 'error': '修改大纲需要切换到 Codex 问答'}
        return
    question = (question or "").strip()
    if not question:
        yield {"type": "error", "error": "问题不能为空"}
        return
    try:
        ctx = context.build_context(video_id)
    except ContextError as e:
        yield {"type": "error", "error": str(e)}
        return

    params = config.settings.qa
    effort = llm_settings.effort_for(STAGE)
    turns = usable_turns(history(video_id))
    hist_ids = {c["id"] for m in turns if m["role"] == "assistant"
                for c in m.get("citations") or [] if c.get("id") in ctx.index}
    _insert(video_id, "user", question, None, version=ctx.version, conversation_id=conversation_id)

    # 第 1 步：定位（不加 response_format，见模块说明）
    yield {"type": "phase", "phase": "locate"}
    try:
        raw = llm.complete(messages=build_messages(turns, context.locate_prompt(question, params)),
                           system=ctx.system, max_tokens=MAX_LOCATE_TOKENS, video_id=video_id, stage=STAGE,
                           cache_system=True, effort=effort)
    except llm.LLMError as e:
        yield {"type": "error", "error": str(e)}
        return
    except Exception as e:  # noqa: BLE001
        log.exception("问答定位失败")
        yield {"type": "error", "error": f"问答失败：{e}"}
        return
    expand_ids, image_ids = context.parse_locate(raw, ctx.index, params)
    exp = context.expand(ctx, expand_ids, image_ids, params)

    # 第 2 步：回答（流式）
    yield {"type": "phase", "phase": "answer", "expanded": {"t": len(exp.t_ids), "images": len(exp.images)}}
    messages = build_messages(turns, context.answer_content(question, exp))
    parts: list[str] = []
    try:
        for text in llm.stream(messages=messages, system=ctx.system, max_tokens=MAX_ANSWER_TOKENS,
                               video_id=video_id, stage=STAGE, cache_system=True, effort=effort):
            parts.append(text)
            yield {"type": "delta", "text": text}
    except llm.LLMError as e:
        yield {"type": "error", "error": str(e)}
        return
    except Exception as e:  # noqa: BLE001
        log.exception("问答失败")
        yield {"type": "error", "error": f"问答失败：{e}"}
        return

    # 第 3 步：引用校验，必要时自动修正一次
    answer = citations.normalize("".join(parts).strip()) or ctx.not_found
    allowed = set(ctx.prefix_ids) | set(exp.ids) | hist_ids
    cites, invalid = citations.resolve(answer, ctx.index, allowed)
    if invalid:
        fixed = _fix_citations(ctx, messages, answer, invalid, allowed, exp.ids, hist_ids, effort)
        if fixed is not None:
            answer = fixed
            yield {"type": "replace", "text": answer}
            cites, invalid = citations.resolve(answer, ctx.index, allowed)
    if invalid:
        log.warning("引用修正后仍有无效 ID（视频 %s）：%s", video_id, ", ".join(invalid))
        _insert(video_id, "assistant", answer, cites, version=ctx.version, status="failed", conversation_id=conversation_id)
        yield {"type": "error", "error": CITATION_ERROR}
        return
    yield {"type": "done", "message": _insert(video_id, "assistant", answer, cites, version=ctx.version, conversation_id=conversation_id)}
