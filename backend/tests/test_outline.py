"""outline（交接 0001 §7）：短索引、结构化生成、校验与重试。LLM 一律 mock：
服务商为 anthropic（tmp_settings），complete_structured 走 `llm.complete(json_mode=True)`，这里替换 `complete`，
这样 complete_structured 自身的「check 不通过 → 带反馈重试」路径也被覆盖。"""
import json

import pytest

from app import artifacts
from app.llm import client as llm, settings as llm_settings
from app.pipeline import outline
from app.pipeline.base import StageContext, StageError


def _t(tid, sec, start, end, text, cat=None):
    return {"id": tid, "section": sec, "start": start, "end": end, "jump": start, "text": text,
            "category": cat, "cue_first": "C0001", "cue_last": "C0002"}


def _sec(sid, page, ds, de, t_ids, summary="", **kw):
    s = {"id": sid, "page": page, "display_start": ds, "display_end": de,
         "speech_start": None, "speech_end": None, "jump": ds, "spoken_summary": summary,
         "t_ids": t_ids, "local_tags": [], "truncated": False, "truncated_t_ids": []}
    s.update(kw)
    return s


def _page(pid, title, vis, sections, jump, status="ok", similar_prev=None):
    return {"id": pid, "title": title, "visual_content": vis, "status": status, "jump": jump,
            "image": f"keyframes/{pid}.jpg", "sections": sections, "similar_prev": similar_prev}


EVIDENCE = {
    "pages": [
        _page("S001", "Intro", "- welcome\n- plan", ["S001.1", "S001.2"], 0.0),
        _page("S002", "Intro", "- welcome\n- plan [handwritten] x", ["S002.1"], 60.0,
              similar_prev={"id": "S001", "score": 0.8}),
        _page("S003", "Other", "- other", ["S003.1"], 120.0, similar_prev={"id": "S002", "score": 0.7}),
        _page("S004", "", "", ["S004.1"], 260.0, status="failed"),
        _page("S005", "Long", "- long slide", ["S005.1"], 300.0),
    ],
    "sections": [
        _sec("S001.1", "S001", 0.0, 60.0, ["T0001"], "Intro summary"),
        _sec("S002.1", "S002", 60.0, 120.0, ["T0002"], "Annotated"),
        _sec("S003.1", "S003", 120.0, 200.0, [], ""),
        {"id": "N001", "page": None, "display_start": None, "display_end": None,
         "speech_start": 200.0, "speech_end": 260.0, "jump": 200.0, "spoken_summary": "",
         "t_ids": ["T0003"], "local_tags": ["media_candidate"]},
        _sec("S004.1", "S004", 260.0, 300.0, ["T0004"]),
        _sec("S005.1", "S005", 300.0, 400.0, ["T0005", "T0006"], "Long summary",
             truncated=True, truncated_t_ids=["T0006"]),
        _sec("S001.2", "S001", 900.0, 960.0, ["T0007"], "Back to intro"),
    ],
    "speech": [
        _t("T0001", "S001.1", 1.0, 50.0, "hello", "lecture"),
        _t("T0002", "S002.1", 61.0, 110.0, "write x", "lecture"),
        _t("T0003", "N001", 200.0, 260.0, "movie clip", None),
        _t("T0004", "S004.1", 261.0, 290.0, "failed page speech", None),
        _t("T0005", "S005.1", 301.0, 350.0, "first half", "lecture"),
        _t("T0006", "S005.1", 350.0, 399.0, "cut half", "uncertain"),
        _t("T0007", "S001.2", 901.0, 950.0, "again", "admin"),
    ],
}


# ---------- 短索引 ----------

def test_short_index_format():
    idx = outline.build_short_index(EVIDENCE)
    lines = idx.splitlines()
    # 按时间排序：S001.2（15:00）排最后
    heads = [ln.split("]")[0][1:] for ln in lines if ln.startswith("[")]
    assert heads == ["S001.1", "S002.1", "S003.1", "N001", "S004.1", "S005.1", "S001.2"]
    assert lines[0] == "[S001.1] 00:00–01:00 | S001 Intro | - welcome ⏎ - plan | Intro summary"
    assert lines[1] == "  T0001 lecture"
    # 同一页的画面内容只给一次
    assert "[S001.2] 15:00–16:00 | S001 Intro | 同 S001 | Back to intro" in lines
    assert idx.count("- welcome ⏎ - plan |") == 1
    # 批注状态提示：标题相同才写
    assert "S002 Intro（可能与 S001 是同一页的不同批注状态） |" in idx
    assert "可能与 S002" not in idx
    # 没有讲解的出现
    assert "[S003.1] 02:00–03:20 | S003 Other | - other | （无讲解）" in lines
    # 无页段：附原文，类别未知不写
    assert "[N001] 03:20–04:20 | 无幻灯片（本机检测" in idx
    assert "  [T0003 03:20–04:20] movie clip" in lines
    # 读图失败：附原文
    assert "[S004.1] 04:20–05:00 | S004（读图失败，以讲解原文代替）" in lines
    assert "  [T0004 04:21–04:50] failed page speech" in lines
    # 截断：概括过的 T 进类别行，被截掉的附原文（带类别）
    i = lines.index("  T0005 lecture")
    assert lines[i + 1].startswith("  （讲解过长")
    assert lines[i + 2] == "  [T0006 05:50–06:39 uncertain] cut half"
    # 每个 T 在索引中恰好出现一次
    for t in EVIDENCE["speech"]:
        assert idx.count(t["id"]) == 1, t["id"]


def test_short_index_skipped_pages_use_raw_text():
    ev = {"pages": [_page("S001", "", "", ["S001.1"], 0.0, status="skipped")],
          "sections": [_sec("S001.1", "S001", 0.0, 30.0, ["T0001"])],
          "speech": [_t("T0001", "S001.1", 0.0, 30.0, "only raw")]}
    idx = outline.build_short_index(ev)
    assert idx.splitlines() == ["[S001.1] 00:00–00:30 | S001（未读图，以讲解原文代替）",
                                "  [T0001 00:00–00:30] only raw"]


def test_short_index_without_raw_text_for_qa():
    idx = outline.build_short_index(EVIDENCE, raw_text=False, extra_categories={"T0003": "media"})
    assert "movie clip" not in idx and "cut half" not in idx
    assert "  T0003 media（未概括，可展开原文）" in idx
    assert "  T0006 uncertain（摘要不完整，可展开原文）" in idx
    assert "  T0004 ?（未概括，可展开原文）" in idx


def test_short_index_placeholders():
    idx = outline.build_short_index(EVIDENCE, placeholder_summaries=True)
    lines = idx.splitlines()
    head = lines[0].split(" | ")
    assert head[1] == "S001 " + " ".join(["word"] * outline.PLACEHOLDER_TITLE_TOKENS)
    assert head[2].split() == ["word"] * outline.PLACEHOLDER_VISUAL_TOKENS
    assert head[3].split() == ["word"] * outline.PLACEHOLDER_SUMMARY_TOKENS
    assert "| 同 S001 |" in idx
    assert "  T0001 lecture" in lines
    # 读图失败的页在估算时按成功处理；截断与无页段仍附原文
    assert "读图失败" not in idx and "failed page speech" not in idx
    assert "cut half" in idx and "movie clip" in idx
    assert "可能与 S001" in idx and "可能与 S002" in idx   # 标题未知，similar_prev 不为空就写


# ---------- 提示词 ----------

def test_output_language_in_system_prompt(tmp_settings):
    system, messages = outline.build_request(EVIDENCE)
    assert "用 简体中文 输出标题和摘要，专有名词保留原文" in system
    assert outline.SHORT_INDEX_LEGEND in system
    assert messages[0]["content"].startswith("以下是本节课的短索引：")
    assert outline.build_short_index(EVIDENCE) in messages[0]["content"]
    llm_settings.update({"output_language": "en"})
    system, _ = outline.build_request(EVIDENCE)
    assert "用 English 输出标题和摘要" in system


# ---------- 校验 ----------

INDEX = outline.id_index(EVIDENCE)


def _resp(chapters, admin_items=(), cats=()):
    return {"chapters": [{"title": t, "summary": "", "evidence": ev,
                          "children": [{"title": ct, "summary": "", "evidence": cev} for ct, cev in ch]}
                         for t, ev, ch in chapters],
            "admin": {"title": "课程事务", "items": [{"title": t, "summary": "", "evidence": ev}
                                                  for t, ev in admin_items]},
            "speech_categories": [{"id": i, "category": c} for i, c in cats]}


def test_validate_fills_start_admin_and_categories():
    raw = _resp([("A", ["S001.1", "S002.1", "X999"], [("a1", ["T0002"])]),
                 ("B", [], [("b1", ["S005.1", "T0006"])])],
                admin_items=[("hw", ["T0007", "BAD"])],
                cats=[("T0003", "media"), ("T0001", "lecture")])
    out, warnings, feedback = outline.validate(raw, INDEX, no_page_t=["T0003"])
    a, b = out["chapters"]
    assert a["evidence"] == ["S001.1", "S002.1"] and a["start"] == 0.0
    assert a["children"][0]["start"] == 61.0 and a["children"][0]["children"] == []
    assert b["start"] == 300.0                         # 没有自身证据时取子节点最早
    item = out["admin"]["items"][0]
    assert item == {"title": "hw", "summary": "", "evidence": ["T0007"], "start": 901.0}
    assert out["speech_categories"] == {"T0003": "media"}
    assert any("X999" in w and "BAD" in w for w in warnings)
    assert any("非无页" in w and "T0001" in w for w in warnings)
    assert len(feedback) == 1 and "X999" in feedback[0]


def test_validate_disorder_and_missing_categories():
    raw = _resp([("late", ["S005"], []), ("early", ["S001"], []), ("x", ["S002"], [])])
    _, warnings, feedback = outline.validate(raw, INDEX, no_page_t=["T0003"])
    assert any("顺序明显错乱" in w for w in warnings) and feedback
    assert any("没有分类" in w and "T0003" in w for w in warnings)


# ---------- 生成与重试 ----------

@pytest.fixture
def fake_llm(tmp_settings, monkeypatch):
    """替换 llm.complete：按顺序返回 replies（dict 会转成 JSON 文本），记录每次请求。"""
    state = {"replies": [], "calls": []}

    def complete(*, messages, system=None, **kw):
        state["calls"].append({"messages": messages, "system": system, **kw})
        r = state["replies"][len(state["calls"]) - 1]
        return r if isinstance(r, str) else json.dumps(r)

    monkeypatch.setattr(llm, "complete", complete)
    return state


def test_invalid_id_feedback_retry_then_success(fake_llm):
    fake_llm["replies"] = [
        _resp([("A", ["BAD1"], [])]),
        _resp([("A", ["S001.1"], []), ("B", ["S005"], [])], cats=[("T0003", "media")]),
    ]
    out = outline.generate(EVIDENCE, video_id=None)
    calls = fake_llm["calls"]
    assert len(calls) == 2
    assert calls[0]["stage"] == "outline" and calls[0]["json_mode"] is True
    assert "用 简体中文 输出" in calls[0]["system"]
    fb = calls[1]["messages"][-1]
    assert fb["role"] == "user" and "BAD1" in fb["content"]
    assert [c["start"] for c in out["chapters"]] == [0.0, 300.0]
    assert out["speech_categories"] == {"T0003": "media"}
    assert out["admin"] == {"title": "课程事务", "items": []}
    assert out["warnings"] == ["1 页读图失败，以讲解原文代替：S004"]


def test_retry_still_bad_keeps_result_with_warnings(fake_llm):
    bad = _resp([("B", ["S005"], []), ("A", ["S001", "NOPE"], [])], cats=[("T0003", "media")])
    fake_llm["replies"] = [bad, bad]
    out = outline.generate(EVIDENCE, video_id=None)
    assert len(fake_llm["calls"]) == 2                 # 只在 complete_structured 内重试一次，外层不再重试
    assert [c["title"] for c in out["chapters"]] == ["B", "A"]
    assert out["chapters"][1]["evidence"] == ["S001"]
    assert any("NOPE" in w for w in out["warnings"])


def test_invalid_format_twice_raises(fake_llm):
    fake_llm["replies"] = ["not json", {"chapters": []}]
    with pytest.raises(StageError, match="格式"):
        outline.generate(EVIDENCE, video_id=None)
    assert len(fake_llm["calls"]) == 2


def test_truncated_output_is_stage_error(tmp_settings, monkeypatch):
    def boom(**kw):
        raise llm.LLMTruncatedError("模型输出被截断")

    monkeypatch.setattr(llm, "complete", boom)
    with pytest.raises(StageError, match="截断"):
        outline.generate(EVIDENCE, video_id=None)


def test_generate_all_skipped_only_raw_text(fake_llm):
    ev = {"pages": [_page("S001", "", "", ["S001.1"], 0.0, status="skipped")],
          "sections": [_sec("S001.1", "S001", 0.0, 30.0, ["T0001"])],
          "speech": [_t("T0001", "S001.1", 0.0, 30.0, "only raw")]}
    fake_llm["replies"] = [_resp([("A", ["T0001"], [])])]
    out = outline.generate(ev, video_id=None)
    assert "  [T0001 00:00–00:30] only raw" in fake_llm["calls"][0]["messages"][0]["content"]
    assert out["chapters"][0]["start"] == 0.0
    assert out["warnings"] == ["幻灯片未读图，大纲只依据讲解原文生成"]


def test_run_writes_outline(fake_llm):
    fake_llm["replies"] = [_resp([("A", ["S001.1"], [])], admin_items=[("hw", ["T0007"])],
                                 cats=[("T0003", "media")])]
    artifacts.write_artifact("vo", "evidence.json", EVIDENCE, {})
    ctx = StageContext(video_id="vo", stage="outline")
    outline.run(ctx)
    data = ctx.read("outline.json")
    assert set(data) == {"chapters", "admin", "speech_categories", "warnings", "revision"}
    assert data["revision"] and data["chapters"][0]["node_id"]
    assert data["admin"]["items"][0]["start"] == 901.0


def test_run_empty_evidence_no_llm(fake_llm):
    for ev in ({"pages": [], "sections": [], "speech": []}, {}):
        artifacts.write_artifact("vo", "evidence.json", ev, {})
        ctx = StageContext(video_id="vo", stage="outline")
        outline.run(ctx)
        data = ctx.read("outline.json")
        assert data["chapters"] == [] and data["admin"]["items"] == [] and data["warnings"]
    assert fake_llm["calls"] == []


def test_run_rejects_old_evidence_format(fake_llm):
    artifacts.write_artifact("vo", "evidence.json", [{"id": "S001", "kind": "slide"}], {})
    with pytest.raises(StageError, match="旧格式"):
        outline.run(StageContext(video_id="vo", stage="outline"))


def test_short_index_from_real_evidence_assembly():
    # 与 E 组的 build_evidence 对齐：slides.json（§5.4）的截断与分类透传到短索引
    from app.pipeline.evidence import build_evidence

    segments = {"sections": [
        {"id": "S001.1", "page": "S001", "display_start": 0.0, "display_end": 60.0, "speech_start": 0.0,
         "speech_end": 58.0, "image": "keyframes/S001.jpg", "t_ids": ["T0001", "T0002"], "local_tags": [],
         "similar_prev": None},
        {"id": "N001", "page": None, "display_start": None, "display_end": None, "speech_start": 70.0,
         "speech_end": 90.0, "image": None, "t_ids": ["T0003"], "local_tags": [], "similar_prev": None}],
        "speech": [{"id": "T0001", "section": "S001.1", "start": 0.0, "end": 30.0, "cue_first": "C0001",
                    "cue_last": "C0002", "text": "part one"},
                   {"id": "T0002", "section": "S001.1", "start": 30.0, "end": 58.0, "cue_first": "C0003",
                    "cue_last": "C0004", "text": "part two"},
                   {"id": "T0003", "section": "N001", "start": 70.0, "end": 90.0, "cue_first": "C0005",
                    "cue_last": "C0006", "text": "no slide"}]}
    slides = [{"id": "S001", "status": "ok", "title": "T", "visual_content": "V",
               "occurrences": [{"id": "S001.1", "spoken_summary": "sum", "truncated": True,
                                "truncated_t_ids": ["T0002"]}],
               "speech_categories": {"T0001": "lecture", "T0002": "uncertain"}, "appearances": [[0, 60]]}]
    ev = build_evidence(segments, slides)
    idx = outline.build_short_index(ev)
    assert idx.splitlines() == [
        "[S001.1] 00:00–01:00 | S001 T | V | sum",
        "  T0001 lecture",
        "  （讲解过长，摘要只概括了前一部分；以下 T 未被概括，附原文）",
        "  [T0002 00:30–00:58 uncertain] part two",
        "[N001] 01:10–01:30 | 无幻灯片",
        "  [T0003 01:10–01:30] no slide",
    ]
    assert set(outline.id_index(ev)) == {"S001", "S001.1", "N001", "T0001", "T0002", "T0003"}
