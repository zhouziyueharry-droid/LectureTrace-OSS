import copy
import dataclasses

import pytest

from app import artifacts, config
from app.pipeline import evidence
from app.pipeline.base import StageContext, StageError

# Synthetic sections cover no-slide speech, repeated slides and empty appearances.
SEGMENTS = {
    "sections": [
        {"id": "N001", "page": None, "display_start": None, "display_end": None,
         "speech_start": 1230.59, "speech_end": 1363.51, "image": None,
         "t_ids": ["T0037", "T0038"], "local_tags": ["media_candidate"], "similar_prev": None},
        {"id": "S057.1", "page": "S057", "display_start": 4375.0, "display_end": 4921.0,
         "speech_start": None, "speech_end": None, "image": "keyframes/S057.jpg",
         "t_ids": [], "local_tags": [], "similar_prev": None},
        {"id": "S090.1", "page": "S090", "display_start": 6383.0, "display_end": 6423.0,
         "speech_start": 6387.6, "speech_end": 6430.2, "image": "keyframes/S090.jpg",
         "t_ids": ["T0169"], "local_tags": [], "similar_prev": None},
        {"id": "S091.1", "page": "S091", "display_start": 6431.0, "display_end": 6523.0,
         "speech_start": 6433.77, "speech_end": 6525.75, "image": "keyframes/S091.jpg",
         "t_ids": ["T0170", "T0171", "T0172"], "local_tags": [],
         "similar_prev": {"id": "S090", "score": 0.62}},
        {"id": "S057.2", "page": "S057", "display_start": 10221.0, "display_end": 10800.0,
         "speech_start": 10232.48, "speech_end": 10233.48, "image": "keyframes/S057_2.jpg",
         "t_ids": ["T0260"], "local_tags": [], "similar_prev": None},
    ],
    "speech": [
        {"id": "T0037", "section": "N001", "start": 1230.59, "end": 1270.19, "cue_first": "C0275",
         "cue_last": "C0283", "text": "This synthetic segment describes a model input."},
        {"id": "T0038", "section": "N001", "start": 1270.67, "end": 1288.75, "cue_first": "C0284",
         "cue_last": "C0289", "text": "This example contains English and 中文 for a mixed-language check."},
        {"id": "T0169", "section": "S090.1", "start": 6387.6, "end": 6430.2, "cue_first": "C1289",
         "cue_last": "C1296", "text": "The blue sample is closer to the green sample."},
        {"id": "T0170", "section": "S091.1", "start": 6433.77, "end": 6469.07, "cue_first": "C1297",
         "cue_last": "C1302", "text": "Compute the cosine of the angle between two vectors."},
        {"id": "T0171", "section": "S091.1", "start": 6469.21, "end": 6500.47, "cue_first": "C1303",
         "cue_last": "C1311", "text": "Perpendicular vectors have a dot product of zero."},
        {"id": "T0172", "section": "S091.1", "start": 6500.79, "end": 6525.75, "cue_first": "C1312",
         "cue_last": "C1317", "text": "Compare the resulting similarity scores."},
        {"id": "T0260", "section": "S057.2", "start": 10232.48, "end": 10233.48, "cue_first": "C2019",
         "cue_last": "C2019", "text": "Yeah."},
    ],
}

# Synthetic slide records cover titles, visual text and speech categories.
SLIDES = [
    {"id": "S057", "status": "ok", "title": "Assignment 1", "visual_content": "- Due date ...",
     "occurrences": [{"id": "S057.1", "spoken_summary": "", "truncated": False, "truncated_t_ids": []},
                     {"id": "S057.2", "spoken_summary": "Brief acknowledgement.", "truncated": False,
                      "truncated_t_ids": []}],
     "speech_categories": {"T0260": "interaction"}, "appearances": [[4375.0, 4921.0], [10221.0, 10800.0]]},
    {"id": "S090", "status": "ok", "title": "Document Distance",
     "visual_content": "- Three synthetic samples: blue, green and orange.",
     "occurrences": [{"id": "S090.1", "spoken_summary": "The blue sample is closest to the green sample.",
                      "truncated": False, "truncated_t_ids": []}],
     "speech_categories": {"T0169": "lecture"}, "appearances": [[6383.0, 6423.0]]},
    {"id": "S091", "status": "ok", "title": "",
     "visual_content": "Visible text fragment at top: blue and green samples",
     "occurrences": [{"id": "S091.1", "spoken_summary": "Cosine similarity is computed as $a^T b$ ...",
                      "truncated": False, "truncated_t_ids": []}],
     "speech_categories": {"T0170": "lecture", "T0171": "lecture", "T0172": "lecture"},
     "appearances": [[6431.0, 6523.0]]},
]


def build(slides=SLIDES):
    return evidence.build_evidence(copy.deepcopy(SEGMENTS), copy.deepcopy(slides))


def by_id(items):
    return {e["id"]: e for e in items}


def test_normal_assembly():
    ev = build()
    assert set(ev) == {"pages", "sections", "speech"}
    # 页按首次出现排序；回访的出现归到同一页
    assert [p["id"] for p in ev["pages"]] == ["S057", "S090", "S091"]
    pages = by_id(ev["pages"])
    assert pages["S057"]["sections"] == ["S057.1", "S057.2"]
    assert pages["S057"]["image"] == "keyframes/S057.jpg"          # 页截图 = 首次出现的截图
    assert pages["S090"] == {
        "id": "S090", "title": "Document Distance", "visual_content": SLIDES[1]["visual_content"],
        "status": "ok", "image": "keyframes/S090.jpg", "sections": ["S090.1"], "similar_prev": None,
        "jump": 6383.0}
    assert pages["S091"]["title"] == "" and pages["S091"]["status"] == "ok"   # 标题允许为空
    assert pages["S091"]["similar_prev"] == {"id": "S090", "score": 0.62}

    # section：时间字段原样，顺序同 segments
    assert [s["id"] for s in ev["sections"]] == [s["id"] for s in SEGMENTS["sections"]]
    secs = by_id(ev["sections"])
    s91 = secs["S091.1"]
    for k in ("display_start", "display_end", "speech_start", "speech_end", "t_ids", "local_tags", "image"):
        assert s91[k] == SEGMENTS["sections"][3][k]
    assert s91["spoken_summary"].startswith("Cosine similarity")
    assert s91["truncated"] is False and s91["truncated_t_ids"] == []
    assert secs["S057.2"]["image"] == "keyframes/S057_2.jpg"        # 出现保留当次截图
    assert secs["S057.1"]["t_ids"] == [] and secs["S057.1"]["speech_start"] is None

    # speech：与 segments 一一对应，category 来自所在页
    assert [t["id"] for t in ev["speech"]] == [t["id"] for t in SEGMENTS["speech"]]
    sp = by_id(ev["speech"])
    assert sp["T0170"] == {"id": "T0170", "section": "S091.1", "start": 6433.77, "end": 6469.07,
                           "jump": 6433.77, "text": SEGMENTS["speech"][3]["text"], "category": "lecture",
                           "cue_first": "C1297", "cue_last": "C1302"}
    assert sp["T0260"]["category"] == "interaction"


def test_jump_rules():
    ev = build()
    pages, secs, sp = by_id(ev["pages"]), by_id(ev["sections"]), by_id(ev["speech"])
    # 页：首次出现的 display_start（S057 回访在 10221，不影响）
    assert pages["S057"]["jump"] == 4375.0
    # 出现：display_start，而不是 speech_start（S090.1 讲解 6387.6 才开始）
    assert secs["S090.1"]["jump"] == 6383.0
    assert secs["S057.2"]["jump"] == 10221.0
    assert secs["S057.1"]["jump"] == 4375.0                          # 没有讲解的出现也能跳
    # 无页段：speech_start
    assert secs["N001"]["jump"] == 1230.59
    # T：自己的 start
    assert all(t["jump"] == t["start"] for t in ev["speech"])
    assert sp["T0169"]["jump"] == 6387.6


def test_no_page_section():
    ev = build()
    n = by_id(ev["sections"])["N001"]
    assert n["page"] is None and n["image"] is None and n["display_start"] is None
    assert n["local_tags"] == ["media_candidate"]
    assert n["spoken_summary"] == "" and n["truncated"] is False and n["truncated_t_ids"] == []
    sp = by_id(ev["speech"])
    assert sp["T0037"]["category"] is None and sp["T0038"]["category"] is None
    # 无页段不产生页
    assert all(p["id"].startswith("S") for p in ev["pages"])


def test_read_slides_skipped():
    ev = build(slides=None)
    assert [p["id"] for p in ev["pages"]] == ["S057", "S090", "S091"]
    for p in ev["pages"]:
        assert p["status"] == "skipped" and p["title"] == "" and p["visual_content"] == ""
        assert p["image"] == f"keyframes/{p['id']}.jpg"
    assert all(s["spoken_summary"] == "" and not s["truncated"] for s in ev["sections"])
    assert all(t["category"] is None for t in ev["speech"])
    # 跳转与结构不受影响
    assert by_id(ev["pages"])["S091"]["similar_prev"] == {"id": "S090", "score": 0.62}
    assert by_id(ev["sections"])["S090.1"]["jump"] == 6383.0


def test_partial_failure():
    slides = copy.deepcopy(SLIDES)
    slides[2] = {"id": "S091", "status": "failed", "title": "", "visual_content": "",
                 "occurrences": [], "speech_categories": {}, "appearances": [[6431.0, 6523.0]]}
    ev = build(slides)
    pages = by_id(ev["pages"])
    assert pages["S091"]["status"] == "failed" and pages["S091"]["visual_content"] == ""
    assert pages["S090"]["status"] == "ok"
    s91 = by_id(ev["sections"])["S091.1"]
    assert s91["spoken_summary"] == "" and s91["t_ids"] == ["T0170", "T0171", "T0172"]
    sp = by_id(ev["speech"])
    assert [sp[t]["category"] for t in ("T0170", "T0171", "T0172")] == [None, None, None]
    assert sp["T0170"]["text"]                                         # 原文仍在，供下游兜底
    assert sp["T0169"]["category"] == "lecture"


def test_failed_page_content_not_leaked():
    """status=failed 的页即使残留了内容也不透出，不伪装成成功。"""
    slides = copy.deepcopy(SLIDES)
    slides[1]["status"] = "failed"
    ev = build(slides)
    p = by_id(ev["pages"])["S090"]
    assert p["title"] == "" and p["visual_content"] == ""
    assert by_id(ev["sections"])["S090.1"]["spoken_summary"] == ""
    assert by_id(ev["speech"])["T0169"]["category"] is None


def test_page_missing_from_slides_counts_as_failed():
    ev = build([s for s in SLIDES if s["id"] != "S090"])
    assert by_id(ev["pages"])["S090"]["status"] == "failed"


def test_truncation_passthrough():
    slides = copy.deepcopy(SLIDES)
    slides[2]["occurrences"][0].update(truncated=True, truncated_t_ids=["T0172"])
    slides[2]["speech_categories"]["T0172"] = "uncertain"
    ev = build(slides)
    s91 = by_id(ev["sections"])["S091.1"]
    assert s91["truncated"] is True and s91["truncated_t_ids"] == ["T0172"]
    assert by_id(ev["speech"])["T0172"]["category"] == "uncertain"
    assert by_id(ev["sections"])["S090.1"]["truncated"] is False


def test_categories_list_format_and_invalid_values():
    """speech_categories 兼容模型返回的数组格式；非法类别置 null。"""
    slides = copy.deepcopy(SLIDES)
    slides[2]["speech_categories"] = [{"id": "T0170", "category": "admin"},
                                      {"id": "T0171", "category": "bogus"}]
    sp = by_id(build(slides)["speech"])
    assert sp["T0170"]["category"] == "admin"
    assert sp["T0171"]["category"] is None and sp["T0172"]["category"] is None


def test_empty_segments():
    assert evidence.build_evidence({"sections": [], "speech": []}, []) == \
        {"pages": [], "sections": [], "speech": []}


def test_id_index():
    ev = build()
    idx = evidence.id_index(ev)
    assert idx["S057"] == {"kind": "page", "jump": 4375.0}
    assert idx["S057.2"] == {"kind": "section", "jump": 10221.0}
    assert idx["N001"] == {"kind": "section", "jump": 1230.59}
    assert idx["T0169"] == {"kind": "speech", "jump": 6387.6}
    assert len(idx) == 3 + 5 + 7
    assert all(evidence.is_ref_id(i) for i in idx)
    assert "S999" not in idx


@pytest.mark.parametrize("s,ok", [
    ("S012", True), ("S1234", True), ("S012.1", True), ("S012.12", True), ("N001", True),
    ("T0031", True), ("T12345", True),
    ("S12", False), ("S012.", False), ("S012.a", False), ("N01", False), ("T031", False),
    ("C0001", False), ("s012", False), ("T0031x", False),
])
def test_is_ref_id(s, ok):
    assert evidence.is_ref_id(s) is ok


def test_id_re_findall():
    text = "见 [S012.2, T0031]、[N001] 以及 S090；C0001 和 T031 不算。"
    assert evidence.ID_RE.findall(text) == ["S012.2", "T0031", "N001", "S090"]
    # ID_PATTERN 不含捕获组，可以拼进更大的正则
    import re
    grp = re.compile(rf"\[\s*({evidence.ID_PATTERN}(?:\s*,\s*{evidence.ID_PATTERN})*)\s*\]")
    assert grp.findall(text) == ["S012.2, T0031", "N001"]


def test_fmt_time_kept():
    assert evidence.fmt_time(6383) == "1:46:23"
    assert evidence.fmt_time(75.4) == "01:15"


def test_run_writes_artifact(data_dir):
    vid = "vev"
    artifacts.write_artifact(vid, "segments.json", SEGMENTS, {})
    artifacts.write_artifact(vid, "slides.json", SLIDES, {})
    msgs = []
    ctx = StageContext(video_id=vid, stage="evidence", on_progress=lambda f, m: msgs.append(m))
    evidence.run(ctx)
    ev = ctx.read("evidence.json")
    assert ev == evidence.build_evidence(SEGMENTS, SLIDES)
    assert artifacts.read_artifact_meta(vid, "evidence.json")["params"] == config.settings.stage_params("evidence")
    assert "3 页" in msgs[-1] and "4 个出现" in msgs[-1] and "1 个无页段" in msgs[-1]


def test_run_without_slides_json(data_dir):
    vid = "vev2"
    artifacts.write_artifact(vid, "segments.json", SEGMENTS, {})
    msgs = []
    evidence.run(StageContext(video_id=vid, stage="evidence", on_progress=lambda f, m: msgs.append(m)))
    ev = artifacts.read_artifact(vid, "evidence.json")
    assert {p["status"] for p in ev["pages"]} == {"skipped"}
    assert "未读取幻灯片" in msgs[-1]


def test_run_read_slides_disabled_ignores_stale_slides(data_dir):
    """read_slides 在设置里被停用时，残留的旧 slides.json 不参与组装。"""
    vid = "vev3"
    artifacts.write_artifact(vid, "segments.json", SEGMENTS, {})
    artifacts.write_artifact(vid, "slides.json", SLIDES, {})
    s = dataclasses.replace(config.settings, disabled_stages=("read_slides",))
    evidence.run(StageContext(video_id=vid, stage="evidence", settings=s))
    ev = artifacts.read_artifact(vid, "evidence.json")
    assert {p["status"] for p in ev["pages"]} == {"skipped"}


def test_run_missing_segments(data_dir):
    with pytest.raises(StageError):
        evidence.run(StageContext(video_id="vev4", stage="evidence"))
