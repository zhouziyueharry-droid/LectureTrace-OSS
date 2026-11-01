"""read_slides（交接 §5）：请求结构、结构化输出、失败处理、断点缓存。LLM 一律 mock。"""
import base64
import dataclasses
import io
import json
import re

import pytest
from PIL import Image

from app import artifacts, config
from app.config import SlideParams
from app.llm import client as llm
from app.pipeline import slides
from app.pipeline.base import StageContext, StageError

VID = "vsl"


# ---------- 输入数据 ----------

def _seg(pages, speech_text=None, revisit=None, no_speech=(), n_section=False):
    """构造 keyframes.json / segments.json / transcript.json。

    pages: 页数；每页一次出现 S00k.1（显示 k*100 … k*100+50），含两个 T。
    revisit: 页 ID，给该页加第 2 次出现（有自己的截图 _2.jpg）。
    no_speech: 这些页的出现没有 T。
    """
    d = artifacts.video_dir(VID) / "keyframes"
    d.mkdir(parents=True, exist_ok=True)
    kfs, sections, speech = [], [], []
    tn = 0

    def add_t(sec_id, start):
        nonlocal tn
        tn += 1
        tid = f"T{tn:04d}"
        text = (speech_text or {}).get(tid, f"speech {tid} about {sec_id}")
        speech.append({"id": tid, "section": sec_id, "start": start, "end": start + 20,
                       "cue_first": "C0001", "cue_last": "C0001", "text": text})
        return tid

    for i in range(1, pages + 1):
        pid = f"S{i:03d}"
        Image.new("RGB", (2560, 1440), "white").save(d / f"{pid}.jpg")
        apps, imgs = [[i * 100.0, i * 100.0 + 50]], [f"keyframes/{pid}.jpg"]
        t_ids = [] if pid in no_speech else [add_t(f"{pid}.1", i * 100.0), add_t(f"{pid}.1", i * 100.0 + 25)]
        sections.append({"id": f"{pid}.1", "page": pid, "display_start": apps[0][0], "display_end": apps[0][1],
                         "speech_start": None, "speech_end": None, "image": imgs[0], "t_ids": t_ids,
                         "local_tags": [], "similar_prev": None})
        if pid == revisit:
            Image.new("RGB", (2560, 1440), "gray").save(d / f"{pid}_2.jpg")
            apps.append([5000.0, 5100.0])
            imgs.append(f"keyframes/{pid}_2.jpg")
            sections.append({"id": f"{pid}.2", "page": pid, "display_start": 5000.0, "display_end": 5100.0,
                             "speech_start": None, "speech_end": None, "image": imgs[1],
                             "t_ids": [add_t(f"{pid}.2", 5000.0)], "local_tags": [], "similar_prev": None})
        kfs.append({"id": pid, "image": f"keyframes/{pid}.jpg", "appearances": apps, "appearance_images": imgs})
    if n_section:
        sections.append({"id": "N001", "page": None, "display_start": None, "display_end": None,
                         "speech_start": 9000.0, "speech_end": 9020.0, "image": None,
                         "t_ids": [add_t("N001", 9000.0)], "local_tags": [], "similar_prev": None})
    sections.sort(key=lambda s: s["display_start"] if s["display_start"] is not None else s["speech_start"])
    speech.sort(key=lambda t: t["start"])
    artifacts.write_artifact(VID, "keyframes.json", kfs, {})
    artifacts.write_artifact(VID, "segments.json", {"sections": sections, "speech": speech}, {})
    artifacts.write_artifact(VID, "transcript.json", {"language": "en", "segments": []}, {})
    return StageContext(video_id=VID, stage="read_slides")


# ---------- 假的结构化调用 ----------

def parse_request(content):
    """从请求内容里还原每页的出现与 T（含被截掉的）。"""
    pages, cur = {}, None
    for b in content:
        if b["type"] != "text":
            continue
        txt = b["text"]
        m = re.match(r"\[(S\d{3})\] screenshot:", txt)
        if m:
            cur = m.group(1)
            pages[cur] = {"occ": [], "t": []}
            continue
        if cur is None:
            continue
        pages[cur]["occ"] += re.findall(r"Occurrence (S\d{3}\.\d+)", txt)
        pages[cur]["t"] += re.findall(r"\[(T\d{4}) ", txt)
        m = re.search(r"truncated: ([T\d, ]+) not shown", txt)
        if m:
            pages[cur]["t"] += [x.strip() for x in m.group(1).split(",")]
    return pages


def good_reply(content, title=lambda pid: f"Title {pid}", category="lecture"):
    return {"slides": [{"id": pid, "title": title(pid), "visual_content": f"- content of {pid}",
                        "occurrences": [{"id": o, "spoken_summary": f"summary {o}"} for o in v["occ"]],
                        "speech_categories": [{"id": t, "category": category} for t in v["t"]]}
                       for pid, v in parse_request(content).items()]}


class FakeLLM:
    """替换 llm.complete_structured：responder(n, content) 返回 dict（回复）或异常；n 为第几次请求（含重试）。
    与真实实现一致：先按 response_model 校验，再跑 check；不合格时带问题重试 retries 次（默认 1），
    仍不合格抛 LLMInvalidOutputError（raw_text 为最后一次回复）。拒答 / 截断不重试。"""

    def __init__(self, monkeypatch, responder=None):
        self.calls = []          # 每次 complete_structured 调用
        self.feedback = []       # 每次带说明重试时的问题列表
        self.requests = 0
        self.responder = responder or (lambda n, content: good_reply(content))
        monkeypatch.setattr(slides.llm, "complete_structured", self)

    def __call__(self, *, messages, response_model, system=None, check=None, retries=1, **kw):
        content = messages[0]["content"]
        self.calls.append({"content": content, "system": system, "model": response_model, **kw})
        for attempt in range(retries + 1):
            self.requests += 1
            out = self.responder(self.requests, content)
            if isinstance(out, Exception):
                raise out
            raw = json.dumps(out)
            try:
                obj = response_model.model_validate(out)
                problems = check(obj) if check else []
            except Exception as e:  # noqa: BLE001
                problems = [str(e)]
            if not problems:
                return obj
            if attempt < retries:
                self.feedback.append(problems)
        raise llm.LLMInvalidOutputError("bad", raw_text=raw, problems=problems)

    def pages(self):
        return [list(parse_request(c["content"])) for c in self.calls]


def _with_slides(set_settings, **kw):
    set_settings(dataclasses.replace(config.settings, slides=dataclasses.replace(config.settings.slides, **kw)))
    return StageContext(video_id=VID, stage="read_slides")


# ---------- 测试 ----------

def test_encode_image_resized(data_dir):
    _seg(1)
    b64 = slides.encode_image(artifacts.video_dir(VID) / "keyframes/S001.jpg", 1280)
    with Image.open(io.BytesIO(base64.b64decode(b64))) as im:
        assert im.size == (1280, 720) and im.format == "JPEG"


def test_request_structure(data_dir, monkeypatch):
    ctx = _seg(2, revisit="S001")
    fake = FakeLLM(monkeypatch)
    slides.run(ctx)
    assert len(fake.calls) == 1
    call = fake.calls[0]
    content = call["content"]
    kinds = [b["type"] for b in content]
    # 页截图 → 出现 1 的 T → 出现 2 的当次截图 → 出现 2 的 T → 下一页
    assert kinds == ["text", "text", "image", "text", "text", "image", "text", "text", "image", "text", "text"]
    assert content[1]["text"] == "[S001] screenshot:"
    assert content[3]["text"].startswith("Occurrence S001.1 (01:40–02:30):\n  [T0001 01:40–02:00] speech T0001")
    assert "[T0002 02:05–02:25]" in content[3]["text"]
    assert content[4]["text"] == "Occurrence S001.2 (1:23:20–1:25:00), screenshot:"
    assert content[6]["text"].startswith("  [T0003 1:23:20–1:23:40]")
    assert content[7]["text"] == "[S002] screenshot:"
    assert content[-1]["text"] == ('Return {"slides": [...]} with exactly one record for each of S001, S002. '
                                   "Write all text fields in English.")
    # 第 2 次出现发的是当次截图（灰），不是页截图（白）
    with Image.open(io.BytesIO(base64.b64decode(content[5]["source"]["data"]))) as im:
        assert im.getpixel((10, 10)) != (255, 255, 255)
    # 提示词：课程语言；ID 枚举；阶段与推理强度
    assert "The lecture is in English. Write title, visual_content and spoken_summary in English" in call["system"]
    assert "[handwritten @S012.2]" in call["system"]
    schema = json.dumps(call["model"].model_json_schema())
    for i in ("S001", "S002", "S001.2", "T0005"):
        assert f'"{i}"' in schema
    assert call["stage"] == "read_slides" and call["effort"] == "low" and call["video_id"] == VID


def test_run_writes_slides_json(data_dir, monkeypatch):
    ctx = _seg(7, n_section=True)
    fake = FakeLLM(monkeypatch)
    msgs = []
    ctx.on_progress = lambda f, m: msgs.append(m)
    slides.run(ctx)
    assert fake.pages() == [["S001", "S002", "S003", "S004", "S005"], ["S006", "S007"]]
    out = ctx.read("slides.json")
    assert [s["id"] for s in out] == [f"S{i:03d}" for i in range(1, 8)]
    assert out[0] == {"id": "S001", "status": "ok", "title": "Title S001", "visual_content": "- content of S001",
                      "occurrences": [{"id": "S001.1", "spoken_summary": "summary S001.1", "truncated": False,
                                       "truncated_t_ids": []}],
                      "speech_categories": {"T0001": "lecture", "T0002": "lecture"},
                      "appearances": [[100.0, 150.0]]}
    # 无页段的 T 不进读图请求
    assert all("N001" not in json.dumps(c["content"]) for c in fake.calls)
    assert "T0015" not in json.dumps(out)
    assert not (ctx.dir / slides.PARTIAL).exists()
    assert msgs[-1] == "幻灯片 7 页读取完成"


def test_truncation_by_whole_segments(data_dir, set_settings, monkeypatch):
    long = "x" * 80
    _seg(1, speech_text={"T0001": long, "T0002": "short"}, revisit="S001")
    # 每行 = 两格缩进 + "[T0001 01:40–02:00] " + 原文；上限只容得下 T0001 一行 + 几个字符，T0002 整条不放
    line1 = len(f"  [T0001 01:40–02:00] {long}")
    ctx = _with_slides(set_settings, max_speech_chars=line1 + 5)
    fake = FakeLLM(monkeypatch)
    slides.run(ctx)
    text = fake.calls[0]["content"][3]["text"]
    assert long in text and "short" not in text and "[T0002 " not in text
    assert "(… truncated: T0002 not shown — categorise them as uncertain)" in text
    out = ctx.read("slides.json")[0]
    assert out["occurrences"][0]["truncated"] is True and out["occurrences"][0]["truncated_t_ids"] == ["T0002"]
    assert out["occurrences"][1]["truncated"] is False and out["occurrences"][1]["truncated_t_ids"] == []
    # 模型给了 lecture，被截掉的 T 仍一律记为 uncertain
    assert out["speech_categories"] == {"T0001": "lecture", "T0002": "uncertain", "T0003": "lecture"}


def test_truncation_keeps_later_segments_out(data_dir, set_settings, monkeypatch):
    # 第 2 条放不下后，后面更短的也不放（按完整 T 从头累计，截断点之后全部略去）
    _seg(1, speech_text={"T0001": "a" * 10, "T0002": "b" * 200})
    line1 = len(f"  [T0001 01:40–02:00] {'a' * 10}")
    ctx = _with_slides(set_settings, max_speech_chars=line1)
    plans = slides.load_plans(ctx)[1]
    assert plans[0].truncated == {"S001.1": ["T0002"]}
    ctx = _with_slides(set_settings, max_speech_chars=line1 - 1)
    assert slides.load_plans(ctx)[1][0].truncated == {"S001.1": ["T0001", "T0002"]}


def test_empty_title_is_ok(data_dir, monkeypatch):
    ctx = _seg(2)
    FakeLLM(monkeypatch, lambda n, c: good_reply(c, title=lambda pid: "" if pid == "S002" else "A"))
    slides.run(ctx)
    out = ctx.read("slides.json")
    assert [(s["status"], s["title"]) for s in out] == [("ok", "A"), ("ok", "")]


def test_partial_page_failure(data_dir, monkeypatch):
    ctx = _seg(3)

    def responder(n, content):
        r = good_reply(content)
        for rec in r["slides"]:
            if rec["id"] == "S002":          # 画面内容为空、缺一个 T：批内与单页重试都不合格
                rec["visual_content"] = " "
                rec["speech_categories"] = rec["speech_categories"][:1]
        return r

    fake = FakeLLM(monkeypatch, responder)
    msgs = []
    ctx.on_progress = lambda f, m: msgs.append(m)
    slides.run(ctx)
    assert fake.pages() == [["S001", "S002", "S003"], ["S002"]]   # 整批一次 + 失败页单页重试一次
    out = ctx.read("slides.json")
    assert [s["status"] for s in out] == ["ok", "failed", "ok"]
    assert out[1] == {"id": "S002", "status": "failed", "title": "", "visual_content": "",
                      "occurrences": [{"id": "S002.1", "spoken_summary": "", "truncated": False,
                                       "truncated_t_ids": []}],
                      "speech_categories": {}, "appearances": [[200.0, 250.0]]}
    assert out[0]["title"] == "Title S001" and out[2]["speech_categories"] == {"T0005": "lecture", "T0006": "lecture"}
    assert "其中 1 页失败（S002）" in msgs[-1]


def test_split_retry_recovers_truncated_batch(data_dir, monkeypatch):
    ctx = _seg(3)
    fake = FakeLLM(monkeypatch, lambda n, c: llm.LLMTruncatedError("截断") if n == 1 else good_reply(c))
    slides.run(ctx)
    assert fake.pages() == [["S001", "S002", "S003"], ["S001"], ["S002"], ["S003"]]
    assert [s["status"] for s in ctx.read("slides.json")] == ["ok"] * 3


@pytest.mark.parametrize("err", [llm.LLMRefusalError("拒答"), llm.LLMTruncatedError("截断")])
def test_refusal_or_truncation_marks_pages_failed(data_dir, monkeypatch, err):
    ctx = _seg(2, revisit="S002")
    fake = FakeLLM(monkeypatch, lambda n, c: err)
    msgs = []
    ctx.on_progress = lambda f, m: msgs.append(m)
    slides.run(ctx)                      # 阶段本身不失败
    assert len(fake.calls) == 3          # 整批 + 两个单页
    out = ctx.read("slides.json")
    assert [s["status"] for s in out] == ["failed", "failed"]
    assert [o["id"] for o in out[1]["occurrences"]] == ["S002.1", "S002.2"]
    assert all(s["title"] == "" and s["visual_content"] == "" and s["speech_categories"] == {} for s in out)
    assert "其中 2 页失败（S001、S002）" in msgs[-1]


def test_single_page_batch_not_split(data_dir, set_settings, monkeypatch):
    _seg(2)
    ctx = _with_slides(set_settings, batch_size=1)
    fake = FakeLLM(monkeypatch, lambda n, c: llm.LLMRefusalError("拒答") if n == 1 else good_reply(c))
    slides.run(ctx)
    assert fake.pages() == [["S001"], ["S002"]]
    assert [s["status"] for s in ctx.read("slides.json")] == ["failed", "ok"]


def test_other_llm_error_fails_stage_and_resumes(data_dir, monkeypatch):
    ctx = _seg(7)
    fake = FakeLLM(monkeypatch, lambda n, c: llm.LLMError("无法连接模型接口") if n == 2 else good_reply(c))
    with pytest.raises(StageError, match="S006–S007 失败：无法连接模型接口"):
        slides.run(ctx)
    partial = artifacts.read_json(ctx.dir / slides.PARTIAL)
    assert sorted(partial["items"]) == ["S001", "S002", "S003", "S004", "S005"]
    assert partial["schema_version"] == config.SCHEMA_VERSION and partial["params"] == ctx.params
    slides.run(ctx)
    assert fake.pages()[2:] == [["S006", "S007"]]      # 第一批没有重发
    assert [s["status"] for s in ctx.read("slides.json")] == ["ok"] * 7


def _interrupted_run(ctx, monkeypatch):
    """第二批中断，留下 S001–S005 的断点缓存。"""
    FakeLLM(monkeypatch, lambda n, c: llm.LLMError("中断") if n == 2 else good_reply(c))
    with pytest.raises(StageError):
        slides.run(ctx)
    assert (ctx.dir / slides.PARTIAL).exists()


def test_partial_discarded_when_params_or_schema_change(data_dir, set_settings, monkeypatch):
    ctx = _seg(7)
    _interrupted_run(ctx, monkeypatch)
    ctx2 = _with_slides(set_settings, max_edge=1024)           # 参数变化：整份丢弃
    fake = FakeLLM(monkeypatch)
    slides.run(ctx2)
    assert fake.pages() == [["S001", "S002", "S003", "S004", "S005"], ["S006", "S007"]]

    _interrupted_run(ctx2, monkeypatch)
    path = ctx2.dir / slides.PARTIAL
    obj = artifacts.read_json(path)
    artifacts.write_json(path, {**obj, "schema_version": obj["schema_version"] - 1})   # 版本不同：丢弃
    fake = FakeLLM(monkeypatch)
    slides.run(ctx2)
    assert len(fake.calls) == 2

    _interrupted_run(ctx2, monkeypatch)
    obj = artifacts.read_json(path)
    artifacts.write_json(path, {**obj, "format": 1})          # v1 缓存格式：丢弃
    fake = FakeLLM(monkeypatch)
    slides.run(ctx2)
    assert len(fake.calls) == 2


def test_partial_items_validated_individually(data_dir, monkeypatch):
    ctx = _seg(7)
    _interrupted_run(ctx, monkeypatch)
    path = ctx.dir / slides.PARTIAL
    obj = artifacts.read_json(path)
    # S001：v1 格式的旧记录；S002：失败记录；S003：分类不在取值范围内 → 这三页重读，S004、S005 复用
    obj["items"]["S001"] = {"id": "S001", "title": "old", "bullets": [], "formulas": [], "figure": "", "emphasis": ""}
    obj["items"]["S002"] = {**obj["items"]["S002"], "status": "failed"}
    obj["items"]["S003"] = {**obj["items"]["S003"], "speech_categories": {"T0005": "lecture", "T0006": "chat"}}
    artifacts.write_json(path, obj)
    fake = FakeLLM(monkeypatch)
    slides.run(ctx)
    assert fake.pages() == [["S001", "S002", "S003"], ["S006", "S007"]]
    assert [s["status"] for s in ctx.read("slides.json")] == ["ok"] * 7


def test_partial_invalid_when_input_changes(data_dir, monkeypatch):
    ctx = _seg(7)
    _interrupted_run(ctx, monkeypatch)
    # segments 重跑后 S002 的讲解变了（ID 不变）：S002 重读，其余复用
    seg = ctx.read("segments.json")
    next(t for t in seg["speech"] if t["id"] == "T0003")["text"] = "changed"
    artifacts.write_artifact(VID, "segments.json", seg, {})
    fake = FakeLLM(monkeypatch)
    slides.run(ctx)
    assert fake.pages() == [["S002"], ["S006", "S007"]]


def test_occurrence_without_speech(data_dir, monkeypatch):
    ctx = _seg(2, no_speech=("S001", "S002"))
    fake = FakeLLM(monkeypatch)
    slides.run(ctx)
    content = fake.calls[0]["content"]
    assert content[3]["text"] == "Occurrence S001.1 (01:40–02:30):\n  (no speech)"
    out = ctx.read("slides.json")
    assert [s["status"] for s in out] == ["ok", "ok"] and out[0]["speech_categories"] == {}


def test_old_keyframes_without_own_screenshot(data_dir, monkeypatch):
    """旧抽帧结果：第 2 次出现没有当次截图（section.image 即页截图）→ 不重复发页截图。"""
    ctx = _seg(1, revisit="S001")
    seg = ctx.read("segments.json")
    seg["sections"][1]["image"] = "keyframes/S001.jpg"
    artifacts.write_artifact(VID, "segments.json", seg, {})
    kfs = ctx.read("keyframes.json")
    kfs[0].pop("appearance_images")
    artifacts.write_artifact(VID, "keyframes.json", kfs, {})
    fake = FakeLLM(monkeypatch)
    slides.run(ctx)
    content = fake.calls[0]["content"]
    assert [b["type"] for b in content].count("image") == 1
    assert content[4]["text"].startswith("Occurrence S001.2 (1:23:20–1:25:00):\n  [T0003 ")


def test_course_language(data_dir, monkeypatch):
    ctx = _seg(1)
    artifacts.write_artifact(VID, "transcript.json", {"language": "zh", "segments": []}, {})
    fake = FakeLLM(monkeypatch)
    slides.run(ctx)
    assert "The lecture is in Chinese. Write title, visual_content and spoken_summary in Chinese" in fake.calls[0]["system"]
    assert fake.calls[0]["content"][-1]["text"].endswith("Write all text fields in Chinese.")
    (ctx.dir / "transcript.json").unlink()
    slides.run(ctx)
    assert "The lecture is in English." in fake.calls[1]["system"]      # 缺失按 en
    assert slides.language_name("en-US") == "English" and slides.language_name("xx") == "xx"


def test_empty_keyframes(data_dir, monkeypatch):
    artifacts.write_artifact(VID, "keyframes.json", [], {})
    fake = FakeLLM(monkeypatch)
    ctx = StageContext(video_id=VID, stage="read_slides")
    slides.run(ctx)                      # 不需要 segments.json
    assert ctx.read("slides.json") == [] and fake.calls == []


def test_missing_inputs(data_dir, monkeypatch):
    fake = FakeLLM(monkeypatch)
    ctx = StageContext(video_id=VID, stage="read_slides")
    with pytest.raises(StageError, match="keyframes.json"):
        slides.run(ctx)
    ctx = _seg(2)
    (ctx.dir / "segments.json").unlink()
    with pytest.raises(StageError, match="segments.json"):
        slides.run(ctx)
    assert fake.calls == []


def test_inconsistent_segments(data_dir, monkeypatch):
    ctx = _seg(2)
    seg = ctx.read("segments.json")
    seg["sections"] = [s for s in seg["sections"] if s["page"] != "S002"]
    artifacts.write_artifact(VID, "segments.json", seg, {})
    FakeLLM(monkeypatch)
    with pytest.raises(StageError, match="页 S002 没有出现记录"):
        slides.run(ctx)


def test_batch_contents_for_estimate(data_dir, monkeypatch):
    ctx = _seg(7, revisit="S003")
    system, contents = slides.batch_contents(ctx)
    assert "The lecture is in English." in system
    assert len(contents) == 2
    assert [b["type"] for b in contents[0]].count("image") == 6    # 5 页 + S003 的第 2 次出现


# ---------- 输出语言检查（软检查，只对英语课程） ----------

SPANISH = ("El profesor explica que la matriz se obtiene multiplicando cada fila por la columna "
           "correspondiente, y luego pide a los estudiantes que voten cuál de las fórmulas es la correcta "
           "para calcular el producto escalar de los dos vectores.")
ENGLISH = ("The lecturer explains that the matrix is obtained by multiplying each row with the matching column, "
           "and then asks the students to vote on which of the formulas is correct for computing the dot product "
           "of the two vectors.")


def _reply_with(content, summary):
    r = good_reply(content)
    for rec in r["slides"]:
        rec["occurrences"][0]["spoken_summary"] = summary
    return r


def test_language_check_retries_once_with_feedback(data_dir, monkeypatch):
    ctx = _seg(2)
    fake = FakeLLM(monkeypatch, lambda n, c: _reply_with(c, SPANISH if n == 1 else ENGLISH))
    slides.run(ctx)
    assert fake.requests == 2 and len(fake.calls) == 1
    assert fake.feedback == [["S001: spoken_summary of S001.1 is not written in English; "
                              "the lecture is in English, write it in English.",
                              "S002: spoken_summary of S002.1 is not written in English; "
                              "the lecture is in English, write it in English."]]
    out = ctx.read("slides.json")
    assert [s["status"] for s in out] == ["ok", "ok"] and out[0]["occurrences"][0]["spoken_summary"] == ENGLISH


def test_language_check_is_soft(data_dir, monkeypatch):
    """重试后仍不是英语：接受（只记警告），不把页判为失败，也不拆单页重试。"""
    ctx = _seg(2)
    fake = FakeLLM(monkeypatch, lambda n, c: _reply_with(c, SPANISH))
    slides.run(ctx)
    assert fake.requests == 2 and len(fake.calls) == 1
    assert [s["status"] for s in ctx.read("slides.json")] == ["ok", "ok"]


def test_language_check_combined_with_structural_problem(data_dir, monkeypatch):
    """结构问题与语言问题同时出现：一起反馈；第二次只剩语言问题时接受。"""
    ctx = _seg(1)

    def responder(n, c):
        r = _reply_with(c, SPANISH)
        if n == 1:
            r["slides"][0]["speech_categories"] = r["slides"][0]["speech_categories"][:1]
        return r

    fake = FakeLLM(monkeypatch, responder)
    slides.run(ctx)
    assert fake.requests == 2
    assert any("missing these IDs: T0002" in p for p in fake.feedback[0])
    assert any("not written in English" in p for p in fake.feedback[0])
    assert ctx.read("slides.json")[0]["status"] == "ok"


def _resp(visual="- content", summary=ENGLISH):
    from app.llm import schemas
    return schemas.SlidesResponse.model_validate({"slides": [{
        "id": "S001", "title": "", "visual_content": visual,
        "occurrences": [{"id": "S001.1", "spoken_summary": summary}], "speech_categories": []}]})


@pytest.mark.parametrize("summary", [
    ENGLISH,
    # 公式、希腊字母、引号、破折号、带重音的人名都不能误判
    r"The lecturer derives $\hat{x} = \frac{x - \mu}{\sigma}$ — “normalising” each feature — and notes that "
    "α, β, γ and ℝ^n are the usual notation; Schrödinger and Gödel are mentioned as a naïve aside about the café.",
    "Short: é ñ 中文",                                                   # 太短，不判断
    "",
])
def test_language_problems_no_false_positive(summary):
    assert slides.language_problems(_resp(summary=summary), "en") == []


def test_language_problems_visual_content_tolerant():
    # 画面内容夹带少量其他文字（例如中文术语）不算；几乎全是中文才算
    visual = "- Attention is all you need\n- 注意力机制 (attention)\n" + r"- $\mathrm{softmax}(QK^T/\sqrt{d})V$"
    assert slides.language_problems(_resp(visual=visual), "en") == []
    zh = "- 本页介绍矩阵乘法的定义以及它在神经网络中的作用和计算复杂度，并且给出了三个具体例子来说明"
    assert slides.language_problems(_resp(visual=zh), "en") == [
        "S001: visual_content does not look like English; the lecture is in English. "
        "Transcribe slide text as written and write descriptions in English."]


def test_language_problems_flags_spanish_and_chinese_only_for_english_courses():
    zh_summary = "老师解释了矩阵乘法的定义：每一行与对应的列相乘再求和，然后让同学们投票选择哪一个公式是正确的点积计算方法。"
    for summary in (SPANISH, zh_summary):
        assert len(slides.language_problems(_resp(summary=summary), "en")) == 1
    assert slides.language_problems(_resp(summary=zh_summary), "en-GB") != []
    # 其他课程语言不检查
    assert slides.language_problems(_resp(summary=ENGLISH), "zh") == []
    assert slides.language_problems(_resp(summary=SPANISH), "es") == []
