"""问答 v2（交接 0001 §10）：缓存前缀、两步流程、展开、引用校验与修正、历史与旧证据。LLM 一律 mock。"""
from __future__ import annotations

import base64
import dataclasses
import io
import json
import sqlite3
from types import SimpleNamespace

import pytest
from PIL import Image

from app import artifacts, config, db, keys
from app.llm import client as llm, openai_compat, settings as llm_settings
from app.qa import chat, codex_chat, context

VID = "abc123def456"  # 符合 VIDEO_ID_RE，API 测试也能用

EVIDENCE = {
    "pages": [
        {"id": "S001", "title": "Batch Norm", "visual_content": "BN: y = gamma * x_hat + beta", "status": "ok",
         "image": "keyframes/S001.jpg", "sections": ["S001.1", "S001.2"], "similar_prev": None, "jump": 10.0},
        {"id": "S002", "title": "", "visual_content": "", "status": "failed",
         "image": "keyframes/S002.jpg", "sections": ["S002.1"], "similar_prev": None, "jump": 90.0},
    ],
    "sections": [
        {"id": "S001.1", "page": "S001", "display_start": 10.0, "display_end": 60.0, "speech_start": 10.0,
         "speech_end": 60.0, "jump": 10.0, "image": "keyframes/S001.1.jpg",
         "spoken_summary": "Explains why BN normalizes activations.", "truncated": True,
         "truncated_t_ids": ["T0002"], "t_ids": ["T0001", "T0002"], "local_tags": []},
        {"id": "N001", "page": None, "display_start": None, "display_end": None, "speech_start": 60.0,
         "speech_end": 90.0, "jump": 60.0, "image": None, "spoken_summary": "", "truncated": False,
         "truncated_t_ids": [], "t_ids": ["T0003"], "local_tags": []},
        {"id": "S002.1", "page": "S002", "display_start": 90.0, "display_end": 120.0, "speech_start": 90.0,
         "speech_end": 120.0, "jump": 90.0, "image": None, "spoken_summary": "", "truncated": False,
         "truncated_t_ids": [], "t_ids": ["T0004"], "local_tags": []},
        {"id": "S001.2", "page": "S001", "display_start": 120.0, "display_end": 150.0, "speech_start": 120.0,
         "speech_end": 150.0, "jump": 120.0, "image": "keyframes/S001.2.jpg",
         "spoken_summary": "Back to BN at test time.", "truncated": False, "truncated_t_ids": [],
         "t_ids": ["T0005"], "local_tags": []},
    ],
    "speech": [
        {"id": "T0001", "section": "S001.1", "start": 10.0, "end": 35.0, "jump": 10.0,
         "text": "Batch norm normalizes each mini batch.", "category": "lecture"},
        {"id": "T0002", "section": "S001.1", "start": 35.0, "end": 60.0, "jump": 35.0,
         "text": "The learned scale gamma restores capacity.", "category": "lecture"},
        {"id": "T0003", "section": "N001", "start": 60.0, "end": 90.0, "jump": 60.0,
         "text": "Homework two is due Friday.", "category": None},
        {"id": "T0004", "section": "S002.1", "start": 90.0, "end": 120.0, "jump": 90.0,
         "text": "Layer norm works per example.", "category": None},
        {"id": "T0005", "section": "S001.2", "start": 120.0, "end": 150.0, "jump": 120.0,
         "text": "At test time use running averages.", "category": "lecture"},
    ],
}
OUTLINE = {
    "chapters": [{"title": "归一化", "summary": "BN 与 LN", "evidence": ["S001.1", "S002.1"], "start": 10.0,
                  "children": [{"title": "BN 推理", "summary": "", "evidence": ["S001.2"], "start": 120.0,
                                "children": []}]}],
    "admin": {"title": "课程事务", "items": [{"title": "作业二", "summary": "周五截止", "evidence": ["T0003"],
                                           "start": 60.0}]},
    "speech_categories": {"T0003": "admin"},
    "warnings": [],
}


def _jpeg(path, w=2000, h=1000):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (w, h), "white").save(path, format="JPEG")


def _write_evidence(evidence=None, created_at="2026-09-28T01:00:00Z"):
    artifacts.write_json(artifacts.video_dir(VID) / "evidence.json",
                         {"schema_version": config.SCHEMA_VERSION, "params": {}, "created_at": created_at,
                          "data": evidence if evidence is not None else EVIDENCE})


def _setup(outline=True, evidence=None):
    _write_evidence(evidence)
    if outline:
        artifacts.write_artifact(VID, "outline.json", OUTLINE, {})
    kf = artifacts.video_dir(VID) / "keyframes"
    for name in ("S001.jpg", "S001.1.jpg", "S001.2.jpg"):
        _jpeg(kf / name)


class FakeLLM:
    """记录 complete / stream 的调用；locate / fixes 依次返回，answer 为流式片段。"""

    def __init__(self, monkeypatch, locate='{"expand": [], "images": []}', answer=("答案 [S001.1]",), fixes=()):
        self.complete_calls: list[dict] = []
        self.stream_calls: list[dict] = []
        self.locate = locate
        self.answer = list(answer)
        self.fixes = list(fixes)
        monkeypatch.setattr(chat.llm, "complete", self._complete)
        monkeypatch.setattr(chat.llm, "stream", self._stream)

    def _complete(self, **kw):
        self.complete_calls.append(kw)
        if len(self.complete_calls) == 1 or "【定位】" in _last_text(kw["messages"]):
            return self.locate
        return self.fixes.pop(0)

    def _stream(self, **kw):
        self.stream_calls.append(kw)
        yield from self.answer


def _last_text(messages) -> str:
    c = messages[-1]["content"]
    return c if isinstance(c, str) else "".join(b.get("text", "") for b in c if b.get("type") == "text")


def _images(messages) -> list[dict]:
    c = messages[-1]["content"]
    return [b for b in c if b.get("type") == "image"] if isinstance(c, list) else []


def _types(events):
    return [e["type"] if e["type"] != "phase" else f"phase:{e['phase']}" for e in events]


# ---------------------------------------------------------------- 上下文

def test_context_stable_and_complete(data_dir):
    _setup()
    c1 = context.build_context(VID)
    c2 = context.build_context(VID)
    assert c1.system == c2.system                      # 缓存前缀逐字节稳定
    assert json.dumps(c1.system, ensure_ascii=False) == json.dumps(c2.system, ensure_ascii=False)
    rules, body = c1.system[0]["text"], c1.system[1]["text"]
    assert "视频中未找到相关内容" in rules and "用简体中文回答" in rules and "【定位】" in rules
    assert context.SHORT_INDEX_LEGEND in body
    # 大纲：章节、小节、课程事务
    assert "1. 归一化（00:10） [S001.1][S002.1]" in body and "1.1 BN 推理（02:00） [S001.2]" in body
    assert "- 作业二（01:00） [T0003]" in body
    # 短索引：全部类别，无页 T 的分类取自 outline.json；截断的出现注明可展开；不附原文
    assert "[S001.1] 00:10–01:00 | S001 Batch Norm | BN: y = gamma * x_hat + beta" in body
    assert "T0002 lecture（摘要不完整，可展开原文）" in body
    assert "T0003 admin（未概括，可展开原文）" in body
    assert "T0004 ?（未概括，可展开原文）" in body
    assert "同 S001" in body
    assert "Homework two" not in body and "restores capacity" not in body
    assert c1.prefix_ids == frozenset({"S001", "S001.1", "S001.2", "N001", "S002", "S002.1",
                                       "T0001", "T0002", "T0003", "T0004", "T0005"})
    assert c1.version == "2026-09-28T01:00:00Z"


def test_context_language_and_missing_outline(data_dir):
    _setup(outline=False)
    llm_settings.update({"output_language": "en"})
    ctx = context.build_context(VID)
    assert "用English回答" in ctx.system[0]["text"]
    assert "# 大纲\n（暂无大纲）" in ctx.system[1]["text"]
    assert ctx.not_found == context.NOT_FOUND["en"]


def test_context_errors(data_dir):
    ev = list(chat.ask(VID, "问题"))
    assert ev == [{"type": "error", "error": "这个视频还没有整理证据，暂时无法问答"}]
    artifacts.write_artifact(VID, "evidence.json", [{"id": "S001", "kind": "slide"}], {})  # v1 列表格式
    with pytest.raises(context.ContextError, match="旧格式"):
        context.build_context(VID)
    assert list(chat.ask(VID, "  "))[0] == {"type": "error", "error": "问题不能为空"}


# ---------------------------------------------------------------- 定位解析与展开

def test_parse_locate_lenient(data_dir):
    _setup()
    idx = context.build_context(VID).index
    p = config.QaParams(max_expand_ids=3, max_expand_images=1)
    text = '好的：\n```json\n{"expand": ["[T0001]", "S999", "S001.1", "T0001", "N001", "T0005"], ' \
           '"images": ["T0001", "N001x", "S001.2", "S001"]}\n```'
    assert context.parse_locate(text, idx, p) == (["T0001", "S001.1", "N001"], ["S001.2"])
    assert context.parse_locate("不需要展开", idx) == ([], [])
    assert context.parse_locate('"S001"', idx) == ([], [])
    assert context.parse_locate('["T0004"]', idx) == (["T0004"], [])
    assert context.parse_locate('{"expand": "T0004", "images": null}', idx) == (["T0004"], [])


def test_expand_limits_and_truncation(data_dir):
    _setup()
    ctx = context.build_context(VID)
    # 出现展开为全部 T；页展开为各次出现的 T；按时间排序展示
    exp = context.expand(ctx, ["S001.2", "S001"], [])
    assert exp.t_ids == ["T0001", "T0002", "T0005"] and exp.truncated == []
    assert exp.text.index("[T0001") < exp.text.index("[T0005 02:00–02:30 | S001.2] At test time")
    assert "已截断" not in exp.text

    # T 数上限：按定位顺序保留
    exp = context.expand(ctx, ["T0004", "S001.1", "T0005"], [], config.QaParams(max_expand_t=2))
    assert exp.t_ids == ["T0001", "T0004"] and exp.truncated == ["S001.1", "T0005"]
    assert "（已截断" in exp.text and "S001.1、T0005" in exp.text

    # 字符上限：第一段总会给出
    exp = context.expand(ctx, ["T0001", "T0002"], [], config.QaParams(max_expand_chars=40))
    assert exp.t_ids == ["T0001"] and exp.truncated == ["T0002"]

    exp = context.expand(ctx, [], [])
    assert exp.t_ids == [] and "（无" in exp.text


def test_expand_images_page_and_occurrence(data_dir):
    _setup()
    ctx = context.build_context(VID)
    # 让页截图与出现截图可区分
    _jpeg(artifacts.video_dir(VID) / "keyframes" / "S001.jpg", 800, 600)
    exp = context.expand(ctx, [], ["S001", "S001.1"])
    assert [i for i, _ in exp.images] == ["S001", "S001.1"]
    sizes = [Image.open(io.BytesIO(base64.b64decode(d))).size for _, d in exp.images]
    assert sizes[0] == (800, 600)                                     # 页截图
    assert sizes[1] == (config.settings.slides.max_edge, config.settings.slides.max_edge // 2)  # 出现截图，已缩放
    # 出现没有当次截图 → 退回页截图；页截图文件不存在 → 跳过
    exp = context.expand(ctx, [], ["S002.1", "S001.2"])
    assert [i for i, _ in exp.images] == ["S001.2"]
    content = context.answer_content("问题", exp)
    assert content[0]["text"].startswith("问题\n\n【展开的证据】")
    assert content[1] == {"type": "text", "text": "[截图 S001.2]"} and content[2]["type"] == "image"


# ---------------------------------------------------------------- 两步流程

def test_ask_two_steps_share_prefix(data_dir, monkeypatch):
    _setup()
    fake = FakeLLM(monkeypatch, locate='{"expand": ["S001.1"], "images": ["S001.2"]}',
                   answer=("BN 归一化 [S001.1, T0002]", "，推理时用滑动平均 [S001.2]。"))
    events = list(chat.ask(VID, "什么是 BN？"))
    assert _types(events) == ["phase:locate", "phase:answer", "delta", "delta", "done"]
    assert events[1] == {"type": "phase", "phase": "answer", "expanded": {"t": 2, "images": 1}}

    loc, ans = fake.complete_calls[0], fake.stream_calls[0]
    expected_system = context.build_context(VID).system
    assert loc["system"] == ans["system"] == expected_system          # 同一前缀
    for kw in (loc, ans):
        assert kw["cache_system"] is True and kw["stage"] == "qa" and kw["video_id"] == VID
    assert "json_mode" not in loc                                     # 定位不加 response_format
    assert loc["effort"] == ans["effort"] == llm_settings.effort_for("qa")
    assert "【定位】" in _last_text(loc["messages"]) and "什么是 BN？" in _last_text(loc["messages"])
    ans_text = _last_text(ans["messages"])
    assert ans_text.startswith("什么是 BN？\n\n【展开的证据】") and "The learned scale gamma" in ans_text
    assert len(_images(ans["messages"])) == 1

    msg = events[-1]["message"]
    assert msg["content"] == "BN 归一化 [S001.1][T0002]，推理时用滑动平均 [S001.2]。"
    assert msg["citations"] == [{"id": "S001.1", "start": 10.0}, {"id": "T0002", "start": 35.0},
                                {"id": "S001.2", "start": 120.0}]
    assert msg["status"] == "ok" and msg["stale"] is False
    hist = chat.history(VID)
    assert [(m["role"], m["status"], m["stale"]) for m in hist] == [("user", "ok", False), ("assistant", "ok", False)]
    assert hist[1]["citations"] == msg["citations"]
    row = db.fetch_one("SELECT evidence_version FROM chat_messages WHERE role='assistant'")
    assert row["evidence_version"] == "2026-09-28T01:00:00Z"

    # 追问：历史只带问答文字，不重发上一轮展开的证据
    list(chat.ask(VID, "再解释一下"))
    loc2 = fake.complete_calls[1]
    assert [m["role"] for m in loc2["messages"]] == ["user", "assistant", "user"]
    assert loc2["messages"][0]["content"] == "什么是 BN？"
    assert "【展开的证据】" not in json.dumps(loc2["messages"][:2], ensure_ascii=False)
    assert loc2["system"] == expected_system

    chat.clear(VID)
    assert chat.history(VID) == []


def test_locate_parse_failure_degrades(data_dir, monkeypatch):
    _setup()
    fake = FakeLLM(monkeypatch, locate="我觉得不需要展开。", answer=("见 [S001.1]",))
    events = list(chat.ask(VID, "问题"))
    assert events[1]["expanded"] == {"t": 0, "images": 0}
    assert "（无" in _last_text(fake.stream_calls[0]["messages"]) and not _images(fake.stream_calls[0]["messages"])
    assert events[-1]["type"] == "done"


def test_openai_requests_share_cache_key_without_response_format(data_dir, set_settings, monkeypatch):
    """走到 openai_compat 的真实请求体：两步的系统消息相同、prompt_cache_key 相同、都没有 response_format。"""
    set_settings(dataclasses.replace(config.settings, llm_provider="openai"))
    keys.update({"openai": "sk-test"})
    _setup()
    calls: list[dict] = []

    def usage():
        return SimpleNamespace(prompt_tokens=100, completion_tokens=10,
                               prompt_tokens_details=SimpleNamespace(cached_tokens=0),
                               completion_tokens_details=SimpleNamespace(reasoning_tokens=0))

    class FakeCompletions:
        def create(self, **req):
            calls.append(req)
            if req.get("stream"):
                return iter([SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="答 [S001]"))],
                                             usage=None),
                             SimpleNamespace(choices=[], usage=usage())])
            msg = SimpleNamespace(content='{"expand": ["T0003"], "images": []}')
            return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")], usage=usage())

    monkeypatch.setattr(openai_compat, "get_client",
                        lambda: SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions())))
    events = list(chat.ask(VID, "作业什么时候交？"))
    assert events[-1]["type"] == "done"
    loc, ans = calls
    assert "stream" not in loc and ans["stream"] is True
    for req in (loc, ans):
        assert "response_format" not in req
        assert "service_tier" not in req                              # 问答默认标准档
        assert req["extra_body"]["prompt_cache_key"] == f"lecturetrace-qa-{VID}"
        assert req["messages"][0]["content"][0]["prompt_cache_breakpoint"] == {"mode": "explicit"}
    assert loc["messages"][0] == ans["messages"][0]                   # 系统前缀逐字节相同
    assert loc["model"] == ans["model"] and loc["reasoning_effort"] == ans["reasoning_effort"]
    assert "Homework two is due Friday." in json.dumps(ans["messages"][-1], ensure_ascii=False)


# ---------------------------------------------------------------- 引用校验与修正

def test_fix_once_success(data_dir, monkeypatch):
    _setup()
    fake = FakeLLM(monkeypatch, answer=("结论 [S001.3]，另见 [T0009]。",), fixes=["结论 [S001.1]，另见 [T0001]。"])
    events = list(chat.ask(VID, "问题"))
    assert _types(events) == ["phase:locate", "phase:answer", "delta", "replace", "done"]
    assert events[3] == {"type": "replace", "text": "结论 [S001.1]，另见 [T0001]。"}
    fix = fake.complete_calls[1]
    fix_text = _last_text(fix["messages"])
    assert "S001.3、T0009" in fix_text and "S001.3 可改用 S001、S001.1、S001.2" in fix_text
    assert fix["messages"][-2] == {"role": "assistant", "content": "结论 [S001.3]，另见 [T0009]。"}
    assert fix["system"] == fake.stream_calls[0]["system"] and fix["cache_system"] is True
    assert "json_mode" not in fix
    msg = events[-1]["message"]
    assert msg["content"] == "结论 [S001.1]，另见 [T0001]。" and msg["status"] == "ok"
    assert [c["id"] for c in msg["citations"]] == ["S001.1", "T0001"]


def test_fix_still_invalid_marks_failed(data_dir, monkeypatch):
    _setup()
    fake = FakeLLM(monkeypatch, answer=("结论 [S009]。",), fixes=["结论 [S009][S001]。"])
    events = list(chat.ask(VID, "问题一"))
    assert _types(events) == ["phase:locate", "phase:answer", "delta", "replace", "error"]
    assert events[-1] == {"type": "error", "error": "回答引用校验失败，请重试"}
    hist = chat.history(VID)
    assert hist[-1]["status"] == "failed" and hist[-1]["error"] == "回答引用校验失败，请重试"
    assert hist[-1]["content"] == "结论 [S009][S001]。"
    assert hist[-1]["citations"] == [{"id": "S001", "start": 10.0}]

    # 失败的一轮（问题与回答）不带入下一轮上下文
    fake.answer = ["好 [S001]"]
    events = list(chat.ask(VID, "问题二"))
    assert events[-1]["type"] == "done"
    assert [m["role"] for m in fake.stream_calls[-1]["messages"]] == ["user"]


def test_fix_request_error_marks_failed(data_dir, monkeypatch):
    _setup()
    fake = FakeLLM(monkeypatch, answer=("结论 [S009]。",))

    def complete(**kw):
        fake.complete_calls.append(kw)
        if len(fake.complete_calls) == 1:
            return "{}"
        raise llm.LLMError("网络错误")

    monkeypatch.setattr(chat.llm, "complete", complete)
    events = list(chat.ask(VID, "问题"))
    assert _types(events) == ["phase:locate", "phase:answer", "delta", "error"]
    assert chat.history(VID)[-1]["status"] == "failed"


def test_history_citations_allowed_in_follow_up(data_dir, monkeypatch):
    """不在短索引里、但上一轮展开并引用过的 ID，追问时沿用不算无效；没有这段历史时算无效。"""
    ev = json.loads(json.dumps(EVIDENCE))
    ev["speech"].append({"id": "T0006", "section": "S001.2", "start": 150.0, "end": 160.0, "jump": 150.0,
                         "text": "Orphan segment.", "category": None})   # 不在任何 t_ids 里 → 不出现在短索引
    _setup(evidence=ev)
    assert "T0006" not in context.build_context(VID).prefix_ids

    fake = FakeLLM(monkeypatch, locate='{"expand": ["T0006"]}', answer=("见 [T0006]",))
    assert list(chat.ask(VID, "第一问"))[-1]["type"] == "done"

    fake.locate = '{"expand": []}'
    events = list(chat.ask(VID, "接着说"))
    assert _types(events)[-1] == "done" and "replace" not in _types(events)
    assert len(fake.complete_calls) == 2                               # 没有修正请求

    chat.clear(VID)
    fake.fixes = ["见 [T0005]"]
    events = list(chat.ask(VID, "接着说"))
    assert "replace" in _types(events) and events[-1]["type"] == "done"


# ---------------------------------------------------------------- 历史与旧证据

def test_stale_messages_after_evidence_rerun(data_dir, monkeypatch):
    _setup()
    fake = FakeLLM(monkeypatch, answer=("旧回答 [S001.1]",))
    list(chat.ask(VID, "旧问题"))
    # 旧版本（v1）遗留的消息：evidence_version 为 NULL
    db.execute("INSERT INTO chat_messages (video_id, conversation_id, role, content, citations_json, created_at)"
               " VALUES (?,?,?,?,?,?)", (VID, codex_chat.state(VID)['conversation_id'], "user", "更旧的问题", None, db.now_iso()))

    _write_evidence(created_at="2026-09-29T00:00:00Z")                 # 证据重跑
    hist = chat.history(VID)
    assert all(m["stale"] for m in hist)
    assert hist[1]["citations"] == [] and hist[1]["status"] == "ok"
    assert hist[2]["status"] == "ok"                                   # 旧行默认 ok

    fake.answer = ["新回答 [S001.1]"]
    events = list(chat.ask(VID, "新问题"))
    assert [m["role"] for m in fake.stream_calls[-1]["messages"]] == ["user"]   # 旧消息不进上下文
    assert events[-1]["message"]["stale"] is False
    hist = chat.history(VID)
    assert [m["stale"] for m in hist] == [True, True, True, False, False]
    assert hist[-1]["citations"] == [{"id": "S001.1", "start": 10.0}]


def test_llm_errors(data_dir, monkeypatch):
    _setup()

    def boom(**kw):
        raise llm.LLMError("未填写 OpenAI API Key，请到设置页填写")

    monkeypatch.setattr(chat.llm, "complete", boom)
    events = list(chat.ask(VID, "问题"))
    assert events == [{"type": "phase", "phase": "locate"},
                      {"type": "error", "error": "未填写 OpenAI API Key，请到设置页填写"}]

    monkeypatch.setattr(chat.llm, "complete", lambda **kw: "{}")

    def boom_stream(**kw):
        yield "半截"
        raise llm.LLMError("流中断")

    monkeypatch.setattr(chat.llm, "stream", boom_stream)
    events = list(chat.ask(VID, "问题"))
    assert _types(events) == ["phase:locate", "phase:answer", "delta", "error"]
    # 两次都没有回答：未回答的问题不带入下一轮
    assert chat.usable_turns(chat.history(VID)) == []


def test_empty_answer_falls_back(data_dir, monkeypatch):
    _setup()
    FakeLLM(monkeypatch, answer=("  ",))
    msg = list(chat.ask(VID, "问题"))[-1]["message"]
    assert msg["content"] == "视频中未找到相关内容" and msg["citations"] == []


def test_db_adds_chat_columns_to_old_database(data_dir):
    path = config.settings.db_path
    path.unlink()
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE chat_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, video_id TEXT NOT NULL,"
                 " role TEXT NOT NULL, content TEXT NOT NULL, citations_json TEXT, created_at TEXT NOT NULL)")
    conn.execute("INSERT INTO chat_messages (video_id, role, content, created_at) VALUES ('v','user','q','x')")
    conn.commit()
    conn.close()
    db.init_db()
    db.init_db()                                                       # 已存在就跳过
    row = db.fetch_one("SELECT * FROM chat_messages")
    assert row["status"] == "ok" and row["evidence_version"] is None


# ---------------------------------------------------------------- HTTP / SSE

def test_sse_event_order_via_api(data_dir, fake_stages, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    db.execute("INSERT INTO videos (id, title, filename, created_at, status) VALUES (?,?,?,?,?)",
               (VID, "t", "t.mp4", db.now_iso(), "done"))
    _setup()
    FakeLLM(monkeypatch, locate='{"expand": ["T0001"]}', answer=("答 [T0001]", "。"))
    with TestClient(app) as client:
        r = client.post(f"/api/videos/{VID}/chat", json={"question": "问题"})
        items = [json.loads(line[5:]) for line in r.text.splitlines() if line.startswith("data:")]
        assert _types(items) == ["phase:locate", "phase:answer", "delta", "delta", "done"]
        assert items[1]["expanded"] == {"t": 1, "images": 0}
        msg = items[-1]["message"]
        assert set(msg) == {"id", "role", "content", "citations", "status", "stale", "created_at"}
        hist = client.get(f"/api/videos/{VID}/chat").json()
        assert [(m["role"], m["status"], m["stale"]) for m in hist] == [("user", "ok", False),
                                                                       ("assistant", "ok", False)]
        assert hist[1]["citations"] == [{"id": "T0001", "start": 10.0}]
