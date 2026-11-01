import dataclasses
import json

import pytest
from PIL import Image

from app import artifacts, config, keys
from app.llm import openai_compat, schemas, settings as llm_settings, tokens
from app.pipeline import outline, send_plan
from app.pipeline.segments import build_segments

VID = "vsp"


def _setup(n_cues=100, n_slides=3, zh_done=0):
    cues = [{"id": f"C{i:04d}", "start": i, "end": i + 1, "text": "x" * 40,
             "zh": "译" if i < zh_done else None} for i in range(n_cues)]
    artifacts.write_artifact(VID, "cues.json", cues, {})
    kfs = []
    d = artifacts.video_dir(VID) / "keyframes"
    d.mkdir(parents=True, exist_ok=True)
    for i in range(1, n_slides + 1):
        Image.new("RGB", (1920, 1080)).save(d / f"S{i:03d}.jpg")
        kfs.append({"id": f"S{i:03d}", "image": f"keyframes/S{i:03d}.jpg", "appearances": [[i, i + 1]]})
    artifacts.write_artifact(VID, "keyframes.json", kfs, {})


@pytest.fixture
def deepl_mode(data_dir, set_settings, monkeypatch):
    """当前默认配置：DeepL 翻译，读幻灯片与大纲未启用。不发真实请求。"""
    set_settings(dataclasses.replace(config.settings, translate=config.TranslateParams(provider="deepl"),
                                     disabled_stages=("read_slides", "evidence", "outline")))
    usage = {"value": None}
    monkeypatch.setattr(send_plan, "deepl_usage", lambda key: usage["value"])
    return usage


def test_deepl_without_key_blocks(deepl_mode):
    _setup()
    plan = send_plan.build_send_plan(VID)
    assert plan["blocking"] == ["未填写 DeepL API Key，请到设置页填写"]
    assert plan["items"][0]["estimated_cost_usd"] is None
    assert plan['cost_complete'] is False


def test_deepl_free_key_costs_nothing_and_shows_usage(deepl_mode):
    keys.update({"deepl": "abc:fx"})
    deepl_mode["value"] = {"used": 1000, "limit": 500_000}
    _setup(n_cues=10, zh_done=5)
    plan = send_plan.build_send_plan(VID)
    tr = plan["items"][0]
    assert tr["provider"] == "DeepL API Free"
    assert tr["estimated_cost_usd"] == 0.0 and tr["chars"] == 200
    assert "本期已用 1,000 / 500,000" in tr["desc"]
    assert plan["blocking"] == [] and plan["deepl_usage"] == {"used": 1000, "limit": 500_000}
    assert plan["translator"] == "DeepL"
    assert plan['cost_complete'] is True


def test_deepl_free_quota_exceeded_blocks(deepl_mode):
    keys.update({"deepl": "abc:fx"})
    deepl_mode["value"] = {"used": 499_950, "limit": 500_000}
    _setup(n_cues=10)
    assert "DeepL 额度不足" in send_plan.build_send_plan(VID)["blocking"][0]


def test_deepl_pro_key_is_billed(deepl_mode):
    keys.update({"deepl": "abc"})
    _setup(n_cues=10, zh_done=5)
    tr = send_plan.build_send_plan(VID)["items"][0]
    assert tr["provider"] == "DeepL API Pro"
    assert tr["estimated_cost_usd"] == round(5 * 40 * send_plan.DEEPL_USD_PER_CHAR, 4)


def test_disabled_stages_listed_as_skipped(deepl_mode):
    keys.update({"deepl": "abc:fx"})
    _setup()
    plan = send_plan.build_send_plan(VID)
    assert [(i["stage"], i["skipped"]) for i in plan["items"]] == [
        ("translate", False), ("read_slides", True), ("outline", True)]
    assert plan["estimated_cost_usd"] == 0 and plan["slide_count"] == 3


def test_llm_translator(data_dir):
    _setup()
    plan = send_plan.build_send_plan(VID)
    assert plan["translator"] == "LLM" and plan["blocking"] == []
    assert plan["items"][0]["estimated_cost_usd"] > 0


def test_image_tokens_scaled(data_dir):
    _setup(n_slides=1)
    p = artifacts.video_dir(VID) / "keyframes" / "S001.jpg"
    # 1920×1080 缩到 1280×720 → 40×23 块 × 1.2（官方切块公式）
    assert send_plan.image_tokens(p, 1280, "gpt-5.6-sol") == 1104
    assert send_plan.image_tokens(p.with_name("missing.jpg"), 1280) == send_plan.DEFAULT_IMAGE_TOKENS


def test_plan_missing_artifacts(data_dir):
    plan = send_plan.build_send_plan("nothing")
    assert plan["text_chars"] == 0 and plan["slide_count"] == 0 and plan["estimated_cost_usd"] == 0


# ---------- 读幻灯片与大纲的估算（交接 §9） ----------

GVID = "vgpt2"


def _gpt_setup(write_segments=True):
    """两页：S001 出现两次（第 2 次有当次截图），S002 一次；中间有一段无页讲解。图片 948×541 → 612 token。"""
    d = artifacts.video_dir(GVID) / "keyframes"
    d.mkdir(parents=True, exist_ok=True)
    for name in ("S001.jpg", "S001_2.jpg", "S002.jpg"):
        Image.new("RGB", (948, 541), "white").save(d / name)
    kfs = [{"id": "S001", "image": "keyframes/S001.jpg", "appearances": [[0, 100], [400, 500]],
            "appearance_images": ["keyframes/S001.jpg", "keyframes/S001_2.jpg"], "similar_prev": None},
           {"id": "S002", "image": "keyframes/S002.jpg", "appearances": [[100, 200]],
            "appearance_images": ["keyframes/S002.jpg"], "similar_prev": {"id": "S001", "score": 0.8}}]
    times = list(range(0, 200, 10)) + list(range(250, 300, 10)) + list(range(400, 500, 10))
    cues = [{"id": f"C{i + 1:04d}", "start": float(t), "end": float(t + 9), "text": f"sentence number {i}.",
             "zh": None} for i, t in enumerate(times)]
    artifacts.write_artifact(GVID, "keyframes.json", kfs, {})
    artifacts.write_artifact(GVID, "cues.json", cues, {})
    seg = build_segments(cues, kfs, config.settings.segment, "en")
    if write_segments:
        artifacts.write_artifact(GVID, "segments.json", seg, {})
    return kfs, cues, seg


@pytest.fixture
def gpt_mode(data_dir, set_settings):
    """OpenAI 兼容服务商、DeepL 翻译；返回 set(max_speech_chars) 用于调整讲解上限。"""
    def _set(max_speech_chars=6000):
        return set_settings(dataclasses.replace(
            config.settings, llm_provider="openai", translate=config.TranslateParams(provider="deepl"),
            slides=dataclasses.replace(config.settings.slides, max_speech_chars=max_speech_chars)))
    _set()
    return _set


def test_read_slides_estimate_images_truncation_and_output(gpt_mode):
    gpt_mode(max_speech_chars=120)
    kfs, cues, seg = _gpt_setup()
    est = send_plan._read_slides_estimate(GVID, kfs, cues)
    # 3 张图：两页的页截图 + S001 第 2 次出现的当次截图
    assert est["image_tokens"] == 3 * 612 and "截图 3 张" in est["desc"] and "分 1 次请求" in est["desc"]
    t_by = {t["id"]: t for t in seg["speech"]}
    n_cut = sum(1 for s in seg["sections"] if s["page"] and send_plan.speech_lines(s, t_by, 120)[1])
    assert n_cut == 3 and "3 个出现的讲解超过 120 字符" in est["desc"]
    # 输出：页数 × 实测系数 × effort=low 区间
    assert est["model"] == "gpt-5.6-sol" and est["effort"] == "low"
    per_page = send_plan.SLIDE_OUTPUT_PER_PAGE["gpt-5.6-sol"]
    assert est["output_range"] == (round(2 * per_page * 0.85), round(2 * per_page * 1.25))
    llm_settings.update({"models": {"read_slides": "gpt-5.6-luna"}})
    est = send_plan._read_slides_estimate(GVID, kfs, cues)
    assert est["output_range"] == (round(2 * 440 * 0.85), round(2 * 440 * 1.25))
    # 截断越多，文字 token 越少
    gpt_mode(max_speech_chars=6000)
    full = send_plan._read_slides_estimate(GVID, kfs, cues)
    assert full["requests"][0][0] > est["requests"][0][0] and "截断" not in full["desc"]


def test_speech_lines_cut_whole_segments():
    sec = {"t_ids": ["T0001", "T0002", "T0003"]}
    t_by = {f"T000{i}": {"id": f"T000{i}", "start": 0.0, "end": 1.0, "text": "a" * 20} for i in (1, 2, 3)}
    line_len = len("  [T0001 00:00–00:01] ") + 20
    lines, cut = send_plan.speech_lines(sec, t_by, 2 * line_len)
    assert len(lines) == 2 and cut == ["T0003"]
    lines, cut = send_plan.speech_lines(sec, t_by, 2 * line_len - 1)
    assert len(lines) == 1 and cut == ["T0002", "T0003"]      # 放不下的 T 整条不放，其后的也不放


def test_outline_estimate_uses_same_short_index(gpt_mode):
    gpt_mode(max_speech_chars=120)
    kfs, cues, seg = _gpt_setup()
    est = send_plan._outline_estimate(GVID, kfs, cues)
    ev = send_plan.placeholder_evidence(seg, 120)
    system, messages = outline.build_request(ev, placeholder_summaries=True)
    idx = outline.build_short_index(ev, placeholder_summaries=True)
    assert idx in messages[0]["content"]
    expected = tokens.request_input_tokens(messages, system, "gpt-5.6-sol")[0] + tokens.text_tokens(
        json.dumps(openai_compat.strict_json_schema(schemas.OutlineResponse)))
    assert est["requests"] == [(expected, 0)]
    # 占位短索引：截掉的 T 与无页段附原文，同一页第 2 次出现写「同 S001」，批注状态提示照写
    assert "[N001]" in idx and "同 S001" in idx and "（讲解过长" in idx and "可能与 S001" in idx
    assert "3 个幻灯片出现、1 个无幻灯片时段" in est["desc"]
    n_sec = len(seg["sections"])
    lo, hi = send_plan.OUTLINE_OUTPUT_PER_SECTION
    r = send_plan.OUTLINE_REASONING["medium"]
    assert est["output_range"] == (n_sec * lo + r[0], n_sec * hi + r[1])


def test_outline_estimate_follows_placeholder_length(gpt_mode, monkeypatch):
    kfs, cues, _ = _gpt_setup()
    base = send_plan._outline_estimate(GVID, kfs, cues)["requests"][0][0]
    monkeypatch.setattr(outline, "PLACEHOLDER_SUMMARY_TOKENS", outline.PLACEHOLDER_SUMMARY_TOKENS + 100)
    # 3 个出现，每个摘要多 100 个占位词
    assert send_plan._outline_estimate(GVID, kfs, cues)["requests"][0][0] == base + 3 * 100


def test_estimate_without_segments_json_matches(gpt_mode):
    kfs, cues, _ = _gpt_setup(write_segments=True)
    with_seg = (send_plan._read_slides_estimate(GVID, kfs, cues), send_plan._outline_estimate(GVID, kfs, cues))
    (artifacts.video_dir(GVID) / "segments.json").unlink()
    without = (send_plan._read_slides_estimate(GVID, kfs, cues), send_plan._outline_estimate(GVID, kfs, cues))
    assert with_seg == without


def test_plan_items_keep_frontend_fields(gpt_mode, monkeypatch):
    keys.update({"deepl": "k:fx", "openai": "sk-test"})
    monkeypatch.setattr(send_plan, "deepl_usage", lambda key: None)
    _gpt_setup()
    plan = send_plan.build_send_plan(GVID)
    items = {i["stage"]: i for i in plan["items"]}
    for st in ("read_slides", "outline"):
        it = items[st]
        assert it["skipped"] is False and it["estimated_cost_usd"] > 0
        for k in ("label", "provider", "desc", "model", "effort", "input_tokens_range", "output_tokens_range",
                  "image_tokens", "cost_usd_range"):
            assert k in it, (st, k)
    assert items["outline"]["image_tokens"] == 0
    assert plan["blocking"] == [] and plan["slide_count"] == 2
