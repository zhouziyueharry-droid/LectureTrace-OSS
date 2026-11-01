"""segments 阶段（交接 §4）：静音切分、句末延后、尾巴合并、无损覆盖、无幻灯片、静音出现、media_candidate、截图回退。"""
from __future__ import annotations

import dataclasses

import pytest

from app import artifacts, config
from app.config import SegmentParams
from app.pipeline import segments
from app.pipeline.base import StageContext, StageError
from app.pipeline.segments import build_segments, ends_sentence, join_texts

P = SegmentParams()


def mk_cues(spec: list[tuple[float, float, str]]) -> list[dict]:
    return [{"id": f"C{i:04d}", "start": a, "end": b, "text": t, "zh": None}
            for i, (a, b, t) in enumerate(spec, 1)]


def page(pid: str, *apps: tuple[float, float], images: list[str] | None = None, **extra) -> dict:
    d = {"id": pid, "image": f"keyframes/{pid}.jpg", "appearances": [list(a) for a in apps], **extra}
    if images is not None:
        d["appearance_images"] = images
    return d


def cue_ids_of(t: dict, cues: list[dict]) -> list[str]:
    ids = [c["id"] for c in cues]
    return ids[ids.index(t["cue_first"]): ids.index(t["cue_last"]) + 1]


def assert_lossless(data: dict, cues: list[dict]) -> None:
    speech = data["speech"]
    assert [c for t in speech for c in cue_ids_of(t, cues)] == [c["id"] for c in cues]
    assert [t["id"] for t in speech] == [f"T{i:04d}" for i in range(1, len(speech) + 1)]
    owners: dict[str, list[str]] = {}
    for s in data["sections"]:
        for tid in s["t_ids"]:
            owners.setdefault(tid, []).append(s["id"])
    for t in speech:
        assert owners[t["id"]] == [t["section"]]


def by_id(data: dict) -> dict[str, dict]:
    return {s["id"]: s for s in data["sections"]}


# ---------- 小工具 ----------

def test_ends_sentence():
    assert ends_sentence("Done.") and ends_sentence('He said "no."') and ends_sentence("真的吗？")
    assert ends_sentence("(see above.)") and ends_sentence("结束。」")
    assert not ends_sentence("so the") and not ends_sentence("well,")


def test_join_texts():
    assert join_texts(["Hello there.", "Next one"]) == "Hello there. Next one"
    assert join_texts(["这是第一句。", "第二句"]) == "这是第一句。第二句"
    assert join_texts(["money.", "现在开始。", "Physics."]) == "money. 现在开始。 Physics."


# ---------- 切 T ----------

def test_silence_split():
    cues = mk_cues([(0, 5, "Hello"), (30, 35, "there.")])   # 静音 25 秒 > 20
    data = build_segments(cues, [page("S001", (0, 100))], P)
    assert [(t["cue_first"], t["cue_last"]) for t in data["speech"]] == [("C0001", "C0001"), ("C0002", "C0002")]
    # 静音后的短尾巴不并入前一个 T
    assert by_id(data)["S001.1"]["t_ids"] == ["T0001", "T0002"]


def test_target_and_max_cut():
    # 每条 5 秒、都以句末结尾：满 30 秒收口
    cues = mk_cues([(i * 5, i * 5 + 5, "x.") for i in range(12)])
    data = build_segments(cues, [page("S001", (0, 100))], P)
    assert [(t["start"], t["end"]) for t in data["speech"]] == [(0, 30), (30, 60)]
    # 始终不在句末：满 90 秒强制收口
    cues = mk_cues([(i * 5, i * 5 + 5, "and") for i in range(40)])
    data = build_segments(cues, [page("S001", (0, 300))], P)
    assert [(t["start"], t["end"]) for t in data["speech"]] == [(0, 90), (90, 180), (180, 200)]


def test_tail_merge():
    cues = mk_cues([(i * 5, i * 5 + 5, "x.") for i in range(13)])   # 0–65：尾巴 60–65 不足 10 秒
    data = build_segments(cues, [page("S001", (0, 100))], P)
    assert [(t["start"], t["end"]) for t in data["speech"]] == [(0, 30), (30, 65)]
    assert_lossless(data, cues)


# ---------- 分段 ----------

def test_sentence_snap_keeps_cue_on_previous_page():
    kfs = [page("S001", (0, 20)), page("S002", (20, 60))]
    cues = mk_cues([(10, 18, "we compute the"), (18, 24, "gradient."), (24, 30, "Next slide.")])
    data = build_segments(cues, kfs, P)
    s1, s2 = by_id(data)["S001.1"], by_id(data)["S002.1"]
    assert [data["speech"][0]["cue_last"]] == ["C0002"] and data["speech"][0]["section"] == "S001.1"
    # 只改归属，不改显示时间
    assert (s1["display_start"], s1["display_end"]) == (0, 20)
    assert (s1["speech_start"], s1["speech_end"]) == (10, 24)
    assert s2["speech_start"] == 24


def test_no_snap_when_sentence_ended_or_too_late():
    kfs = [page("S001", (0, 20)), page("S002", (20, 60))]
    # 前一条已在句末：不延后
    data = build_segments(mk_cues([(10, 18, "Done."), (18, 24, "gradient.")]), kfs, P)
    assert data["speech"][1]["section"] == "S002.1"
    # 新 cue 在边界后 7 秒才结束（> 5 秒）：不延后
    data = build_segments(mk_cues([(10, 18, "we compute the"), (18, 27, "gradient.")]), kfs, P)
    assert data["speech"][1]["section"] == "S002.1"


def test_gap_absorb_only_extends_assignment():
    # 空档 10 秒 < 15：空档里的 cue 归前一页，显示时间不变
    kfs = [page("S001", (0, 20)), page("S002", (30, 60))]
    data = build_segments(mk_cues([(22, 26, "In the gap.")]), kfs, P)
    assert data["speech"][0]["section"] == "S001.1"
    assert by_id(data)["S001.1"]["display_end"] == 20
    # 空档 20 秒 ≥ 15：成为无页段
    kfs = [page("S001", (0, 20)), page("S002", (40, 60))]
    data = build_segments(mk_cues([(25, 29, "In the gap.")]), kfs, P)
    assert data["speech"][0]["section"] == "N001"
    assert [s["id"] for s in data["sections"]] == ["S001.1", "N001", "S002.1"]


def test_silent_appearance_kept_and_silent_gap_dropped():
    kfs = [page("S001", (0, 20)), page("S002", (100, 130)), page("S003", (130, 200))]
    cues = mk_cues([(1, 5, "Hi."), (140, 150, "Later.")])
    data = build_segments(cues, kfs, P)
    s2 = by_id(data)["S002.1"]
    assert s2["t_ids"] == [] and s2["speech_start"] is None and s2["speech_end"] is None
    assert (s2["display_start"], s2["display_end"]) == (100, 130)
    # 20–100 秒无页、无 cue：不产生记录
    assert not any(s["page"] is None for s in data["sections"])


def test_no_slides_all_n_section():
    cues = mk_cues([(0, 5, "One."), (6, 10, "Two."), (200, 205, "Three.")])
    for kfs in ([], None):
        data = build_segments(cues, kfs, P)
        assert [s["id"] for s in data["sections"]] == ["N001"]
        n = data["sections"][0]
        assert n["page"] is None and n["display_start"] is None and n["image"] is None
        assert n["local_tags"] == [] and n["similar_prev"] is None
        assert (n["speech_start"], n["speech_end"]) == (0, 205)
        assert len(data["speech"]) == 2          # 静音 190 秒硬切 T，但不切出新的 N
        assert_lossless(data, cues)


def test_no_cues():
    data = build_segments([], [page("S001", (0, 20))], P)
    assert data["speech"] == [] and by_id(data)["S001.1"]["t_ids"] == []
    assert build_segments([], [], P) == {"sections": [], "speech": []}


def test_lossless_on_mixed_lecture():
    kfs = [page("S001", (10, 50), (300, 330)), page("S002", (52, 140)), page("S003", (180, 260))]
    spec, t = [], 0.0
    while t < 400:
        dur = 3 + (int(t) % 5)
        spec.append((t, t + dur, "so and" if int(t) % 3 else "end."))
        t += dur + (25 if 150 < t < 160 else 0.5)
    cues = mk_cues(spec)
    data = build_segments(cues, kfs, P)
    assert_lossless(data, cues)
    ids = [s["id"] for s in data["sections"]]
    assert {"S001.1", "S001.2", "S002.1", "S003.1"} <= set(ids)
    assert all(t["end"] - t["start"] <= P.t_max_s + 10 for t in data["speech"])


# ---------- 标签与透传 ----------

def test_media_candidate():
    kfs = [page("S001", (0, 20)), page("S002", (100, 130))]
    cues = mk_cues([(1, 5, "Hi."), (40, 45, "现在，把箱子打开。"), (110, 115, "Back.")])
    data = build_segments(cues, kfs, P, "en")
    assert by_id(data)["N001"]["local_tags"] == ["media_candidate"]
    assert by_id(data)["S001.1"]["local_tags"] == []
    # 中文课程里的中文不算外语；英文术语也不算
    assert by_id(build_segments(cues, kfs, P, "zh"))["N001"]["local_tags"] == []
    cues_en = mk_cues([(40, 45, "gradient descent, α and β.")])
    assert build_segments(cues_en, kfs, P, "zh-CN")["sections"][1]["local_tags"] == []
    assert build_segments(cues_en, kfs, P, "en")["sections"][1]["local_tags"] == []
    # 有页 section 不打标签
    data = build_segments(mk_cues([(1, 5, "你好。")]), kfs, P, "en")
    assert by_id(data)["S001.1"]["local_tags"] == []


def test_appearance_images_and_fallback():
    cues = mk_cues([(1, 5, "Hi."), (51, 55, "Again.")])
    kfs = [page("S001", (0, 20), (50, 60), images=["keyframes/S001.jpg", "keyframes/S001_2.jpg"]),
           page("S002", (20, 50))]
    d = by_id(build_segments(cues, kfs, P))
    assert d["S001.1"]["image"] == "keyframes/S001.jpg"
    assert d["S001.2"]["image"] == "keyframes/S001_2.jpg"
    assert d["S002.1"]["image"] == "keyframes/S002.jpg"
    # 旧 keyframes.json 没有 appearance_images：退回页的 image
    kfs[0].pop("appearance_images")
    d = by_id(build_segments(cues, kfs, P))
    assert d["S001.2"]["image"] == "keyframes/S001.jpg"


def test_appearances_numbered_by_time():
    kfs = [page("S001", (50, 60), (0, 20), images=["keyframes/S001_2.jpg", "keyframes/S001.jpg"]),
           page("S002", (20, 50))]
    d = by_id(build_segments([], kfs, P))
    assert (d["S001.1"]["display_start"], d["S001.1"]["image"]) == (0, "keyframes/S001.jpg")
    assert (d["S001.2"]["display_start"], d["S001.2"]["image"]) == (50, "keyframes/S001_2.jpg")


def test_similar_prev_passthrough():
    sim = {"id": "S001", "score": 0.8}
    kfs = [page("S001", (0, 20), similar_prev=None), page("S002", (20, 40), (60, 80), similar_prev=sim)]
    d = by_id(build_segments([], kfs, P))
    assert d["S001.1"]["similar_prev"] is None
    assert d["S002.1"]["similar_prev"] == sim and d["S002.2"]["similar_prev"] == sim


def test_params_are_used():
    cues = mk_cues([(0, 5, "Hello"), (30, 35, "there.")])
    p = dataclasses.replace(P, silence_split_s=30.0)
    data = build_segments(cues, [page("S001", (0, 100))], p)
    assert len(data["speech"]) == 1


def test_section_format():
    cues = mk_cues([(1, 5, "Hi.")])
    s = build_segments(cues, [page("S001", (0, 20))], P)["sections"][0]
    assert list(s) == ["id", "page", "display_start", "display_end", "speech_start", "speech_end",
                       "image", "t_ids", "local_tags", "similar_prev"]
    t = build_segments(cues, [page("S001", (0, 20))], P)["speech"][0]
    assert t == {"id": "T0001", "section": "S001.1", "start": 1, "end": 5,
                 "cue_first": "C0001", "cue_last": "C0001", "text": "Hi."}


# ---------- run(ctx) ----------

def test_run_writes_artifact(data_dir):
    vid = "vseg"
    artifacts.write_artifact(vid, "cues.json", mk_cues([(1, 5, "Hi."), (40, 45, "现在。")]), {})
    artifacts.write_artifact(vid, "keyframes.json", [page("S001", (0, 20))], {})
    artifacts.write_artifact(vid, "transcript.json", {"language": "en", "segments": []}, {})
    msgs: list[str] = []
    ctx = StageContext(video_id=vid, stage="segments", on_progress=lambda f, m: msgs.append(m))
    segments.run(ctx)
    data = ctx.read("segments.json")
    assert [s["id"] for s in data["sections"]] == ["S001.1", "N001"]
    assert artifacts.read_artifact_meta(vid, "segments.json")["params"] == \
        config.settings.stage_params("segments")
    assert "1 个出现" in msgs[-1] and "影片" in msgs[-1]


def test_run_without_keyframes_or_language(data_dir):
    vid = "vseg2"
    artifacts.write_artifact(vid, "cues.json", mk_cues([(1, 5, "Hi.")]), {})
    msgs: list[str] = []
    ctx = StageContext(video_id=vid, stage="segments", on_progress=lambda f, m: msgs.append(m))
    segments.run(ctx)
    assert [s["id"] for s in ctx.read("segments.json")["sections"]] == ["N001"]
    assert "没有抽帧结果" in msgs[-1] and "按英文处理" in msgs[-1]


def test_run_without_cues_fails(data_dir):
    with pytest.raises(StageError):
        segments.run(StageContext(video_id="vseg3", stage="segments"))
