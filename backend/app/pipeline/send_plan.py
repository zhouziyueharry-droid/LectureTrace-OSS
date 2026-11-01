"""发送清单（ARCHITECTURE D10）：外部 API 阶段将发送的内容与预估费用。

除查询 DeepL 本月用量外不调外部接口。blocking 非空时不允许确认发送。
"""
from __future__ import annotations

import json
import logging
import math

from PIL import Image

from .. import artifacts, config, keys
from ..llm import settings as llm_settings, tokens
from . import runner
from ..llm import client as llm
from ..keys import DEEPL_USD_PER_CHAR, GOOGLE_USD_PER_CHAR, is_free_key
from .evidence import fmt_time

log = logging.getLogger(__name__)

CHARS_PER_TOKEN = 4.0           # 英文约 4 字符 / token
ZH_TOKENS_PER_EN_TOKEN = 1.3    # 中文译文 token 数相对原文的倍数（粗估）
DEFAULT_IMAGE_TOKENS = 1440     # 读不到尺寸时按 1280×960 估算（40×30 块 × 1.2）

STAGE_LABELS = {"translate": "翻译字幕", "read_slides": "读取幻灯片", "outline": "生成大纲"}


def _read(video_id: str, name: str) -> list:
    try:
        return artifacts.read_artifact(video_id, name) or []
    except FileNotFoundError:
        return []


def image_tokens(path, max_edge: int, model: str = "") -> int:
    """按发送时的缩放（长边 ≤ max_edge）和官方 32×32 切块公式（tokens.image_tokens）估算一张图片的 token。"""
    if model == llm_settings.DEEPSEEK_MODEL:
        return tokens.image_tokens(1, 1, model)
    try:
        with Image.open(path) as im:
            w, h = im.size
    except Exception:  # noqa: BLE001
        return DEFAULT_IMAGE_TOKENS
    scale = min(1.0, max_edge / max(w, h, 1))
    return tokens.image_tokens(max(1, round(w * scale)), max(1, round(h * scale)), model)


def deepl_usage(api_key: str) -> dict | None:
    """查询 DeepL 账户本期用量；失败返回 None（不阻止发送）。"""
    try:
        import deepl

        u = deepl.DeepLClient(api_key).get_usage()
        if u.character is None or not u.character.valid:
            return None
        return {"used": u.character.count, "limit": u.character.limit}
    except Exception as e:  # noqa: BLE001
        log.warning("查询 DeepL 用量失败：%s", e)
        return None


def _translate_item(cues: list, plan: dict) -> dict:
    s = config.settings
    pending = [c for c in cues if not (c.get("zh") or "").strip()]
    chars = sum(len(c.get("text") or "") for c in pending)
    item = {"stage": "translate", "label": STAGE_LABELS["translate"], "skipped": False,
            "desc": f"字幕原文 {len(cues)} 条（待翻译 {len(pending)} 条），约 {chars:,} 字符", "chars": chars}

    provider = s.translation_provider()
    if provider == 'ollama':
        from .ollama_translate import check_model
        from .base import StageError
        if pending:
            try:
                check_model()
            except StageError as error:
                plan['blocking'].append(str(error))
        return {**item, 'provider': f'Ollama 本机 · {s.ollama_translate.model}', 'local': True,
                'estimated_cost_usd': 0.0, 'skipped': not pending}
    if provider == 'google':
        item['provider'] = 'Google Cloud Translation'
        if not pending:
            return {**item, 'estimated_cost_usd': 0.0, 'skipped': True}
        if not keys.get_key('google'):
            plan['blocking'].append('未填写 Google Translation API Key，请到设置页填写')
            return {**item, 'estimated_cost_usd': None}
        plan['warnings'].append('Google 按 NMT 标准价 $20 / 百万字符估算，未扣免费抵扣；剩余额度未知，实际以 Google 账单为准。')
        return {**item, 'estimated_cost_usd': round(chars * GOOGLE_USD_PER_CHAR, 4)}
    if provider != "deepl":
        batches = math.ceil(len(pending) / max(1, s.translate.batch_size))
        ctx_ratio = 1 + 2 * s.translate.context_cues / max(1, s.translate.batch_size)
        t_in = chars / CHARS_PER_TOKEN * ctx_ratio * 1.3 + batches * 500
        t_out = chars / CHARS_PER_TOKEN * ZH_TOKENS_PER_EN_TOKEN + len(pending) * 8
        return {**item, "provider": f"LLM（{s.llm_model}）",
                "estimated_cost_usd": round(llm.estimate_cost(s.llm_model, int(t_in), int(t_out)), 4)}

    key = keys.get_key("deepl")
    item["provider"] = "DeepL"
    if not pending:  # 已全部译完，本次不会发送
        return {**item, "desc": f"字幕 {len(cues)} 条已全部翻译，无需发送", "estimated_cost_usd": 0.0, "skipped": True}
    if not key:
        plan["blocking"].append("未填写 DeepL API Key，请到设置页填写")
        item["estimated_cost_usd"] = None
        return item
    free = is_free_key(key)
    item["provider"] = "DeepL API Free" if free else "DeepL API Pro"
    item["estimated_cost_usd"] = 0.0 if free else round(chars * DEEPL_USD_PER_CHAR, 4)
    if not pending:
        return item
    usage = deepl_usage(key)
    plan["deepl_usage"] = usage
    if usage and usage["limit"]:
        left = usage["limit"] - usage["used"]
        item["desc"] += f"；本期已用 {usage['used']:,} / {usage['limit']:,} 字符"
        if chars > left:
            msg = f"DeepL 额度不足：还剩 {left:,} 字符，本次需要约 {chars:,} 字符"
            (plan["blocking"] if free else plan["warnings"]).append(msg)
    return item


# ---------- GPT 阶段的 token 估算 ----------

# 读幻灯片的输出（含讲解摘要、T 分类和推理），每页的**总计费 output** token，effort=low 时的实测值。
# 来源：v2 付费实验（2026-09-27，S086–S095 共 10 页，2 批 × 5 页，effort=low），
# docs/proposals/0001-experiments/v2_exp/result_v2.json：Luna 2167 + 2211 → 437.8 / 页；Sol 2550 + 2277 → 482.7 / 页。
# 交接 §9 要求写进设置；config.py 不归本组修改，且 send_plan 不是阶段（系数不进产物 params），先放模块常量。
# 2026-09-28 整节课验证（v2_full/result_full.json，Sol，132 页，27 批，flex）：46243 → 350 / 页。
# 小样本页面偏密（483），整节课偏疏（350）；Sol 取 400，配合 low 的 0.85–1.25 覆盖 340–500。Luna 只有 10 页实测，暂不改。
# gpt-6-sol：2026-09-28 实际服务器整节课 37052 → 281 / 页，取 300（low 区间 255–375）。
SLIDE_OUTPUT_PER_PAGE = {"gpt-5.6-luna": 440, "gpt-5.6-sol": 400, "gpt-6-sol": 300}
SLIDE_OUTPUT_PER_PAGE_DEFAULT = 440      # 未实测的模型取两者之间
# 相对 effort=low 实测值的倍数区间。low 的区间覆盖实验里各批的波动（Sol 455–510 / 页）；
# 其他强度未实测，按经验放宽（首次真实运行后可按 api_usage 校准）
SLIDE_EFFORT_FACTOR = {
    "none": (0.6, 1.0), "low": (0.85, 1.25), "medium": (1.1, 2.5),
    "high": (1.5, 4.0), "xhigh": (2.0, 6.0), "max": (2.5, 8.0)}
# 大纲输出 = 固定部分（OUTLINE_REASONING，含推理与整体结构）+ 每个出现 / 无页段一份。
# 2026-09-28 实测（medium，现行写法：短索引含画面转录、以幻灯片为骨架）：整节课 137 个出现 → Sol 9161 token；
# 小样本 12 个出现 → Luna 2232、Sol 1442。区间覆盖以上三次实测。
OUTLINE_OUTPUT_PER_SECTION = (20, 60)
OUTLINE_REASONING = {                    # 生成大纲：固定部分的 token 区间（medium 实测，其余按比例经验值）
    "none": (0, 0), "low": (500, 2500), "medium": (1000, 4000),
    "high": (5000, 25000), "xhigh": (10000, 40000), "max": (15000, 60000)}


def _load_segments(video_id: str, keyframes: list, cues: list) -> dict:
    """segments.json（发送确认时 segments 阶段已完成）；缺失时按当前参数在本机现算，结果相同。"""
    try:
        seg = artifacts.read_artifact(video_id, "segments.json")
    except FileNotFoundError:
        seg = None
    if isinstance(seg, dict) and "sections" in seg:
        return seg
    from .segments import build_segments

    tr = _read(video_id, "transcript.json")
    lang = tr.get("language") if isinstance(tr, dict) else None
    cues = sorted(({**c, "id": c.get("id") or f"C{i + 1:04d}"} for i, c in enumerate(cues)),
                  key=lambda c: c["start"])
    return build_segments(cues, keyframes, config.settings.segment, lang or "en")


def speech_lines(section: dict, t_by: dict, max_chars: int) -> tuple[list[str], list[str]]:
    """一个出现在读图请求里的讲解行，按 §5.1 截断：累计字符超过 max_chars 后，其余 T 整条不放。

    返回 (放进请求的行, 被截掉的 T ID)。直接调用 slides.speech_lines，与真实请求一致。
    """
    from . import slides  # 与真实读图请求同一个截断实现，避免两处漂移

    return slides.speech_lines([t for t in section.get("t_ids") or [] if t in t_by], t_by, max_chars)


def placeholder_evidence(segments: dict, max_chars: int) -> dict:
    """读图之前的 evidence.json：`evidence.build_evidence(segments, None)`（读图跳过）再补上按 §5.1 规则
    预先算好的截断（truncated_t_ids），供 outline.build_request(placeholder_summaries=True) 估算。

    标题、画面内容、摘要、类别由占位模式填固定长度；结构、时间、T 原文与真实生成相同，截断的出现同样附被截掉的原文。
    """
    from .evidence import build_evidence

    ev = build_evidence(segments, None)
    t_by = {t["id"]: t for t in ev["speech"]}
    for s in ev["sections"]:
        if s.get("page") is not None:
            cut = speech_lines(s, t_by, max_chars)[1]
            s["truncated"], s["truncated_t_ids"] = bool(cut), cut
    return ev


def _read_slides_estimate(video_id: str, keyframes: list, cues: list) -> dict:
    """输入 = 系统提示 + 各出现讲解按 max_speech_chars 截断后的文字 + 图片（页截图，第 2 次起的出现附当次截图）。
    输出 = 页数 × 每页实测系数 × 思考强度倍数区间。"""
    from . import slides

    model = llm_settings.model_for("read_slides")
    effort = llm_settings.effort_for("read_slides")
    p = config.settings.slides
    segments = _load_segments(video_id, keyframes, cues)
    t_by = {t["id"]: t for t in segments.get("speech") or []}
    by_page: dict[str, list] = {}
    for s in segments.get("sections") or []:
        if s.get("page") is not None:
            by_page.setdefault(s["page"], []).append(s)
    vdir = artifacts.video_dir(video_id)
    system_tokens = tokens.text_tokens(getattr(slides, "SYSTEM", "")) + tokens.MESSAGE_OVERHEAD
    size = max(1, p.batch_size)
    requests: list[tuple[int, int]] = []
    n_imgs = n_cut = chars = 0
    for i in range(0, len(keyframes), size):
        batch = keyframes[i:i + size]
        parts, img = [f"This batch has {len(batch)} pages."], 0
        for kf in batch:
            img += image_tokens(vdir / kf["image"], p.max_edge, model)
            n_imgs += 1
            parts.append(f"[{kf['id']}] screenshot:")
            for s in sorted(by_page.get(kf["id"], []), key=lambda s: s["display_start"]):
                k = int(s["id"].rsplit(".", 1)[1])
                if k > 1 and s.get("image"):      # 第 2 次起附当次截图（§5.1）
                    img += image_tokens(vdir / s["image"], p.max_edge, model)
                    n_imgs += 1
                lines, cut = speech_lines(s, t_by, p.max_speech_chars)
                n_cut += bool(cut)
                chars += sum(len(x) for x in lines)
                parts.append(f"Occurrence {s['id']} ({fmt_time(s['display_start'])}–{fmt_time(s['display_end'])}):")
                parts += lines or ["  (no speech)"]
                if cut:
                    parts.append(f"  (truncated: {', '.join(cut)} not shown)")
        parts.append('Return {"slides": [...]} with exactly one record for each page.')
        requests.append((system_tokens + tokens.MESSAGE_OVERHEAD + tokens.text_tokens("\n".join(parts)), img))
    n = len(keyframes)
    per_page = SLIDE_OUTPUT_PER_PAGE.get(model, SLIDE_OUTPUT_PER_PAGE_DEFAULT)
    f = SLIDE_EFFORT_FACTOR.get(effort, SLIDE_EFFORT_FACTOR["medium"])
    out = (round(n * per_page * f[0]), round(n * per_page * f[1]))
    img_total = sum(r[1] for r in requests)
    desc = (f"幻灯片截图 {n_imgs} 张（共 {img_total:,} 图片 token）及对应讲解原文（约 {chars:,} 字符），"
            f"分 {len(requests)} 次请求")
    if n_cut:
        desc += f"；{n_cut} 个出现的讲解超过 {p.max_speech_chars:,} 字符，按整段截断"
    return {"model": model, "effort": effort, "requests": requests, "output_range": out, "image_tokens": img_total,
            "desc": desc}


def _outline_estimate(video_id: str, keyframes: list, cues: list) -> dict:
    """用与真实生成相同的请求构造（outline.build_request；标题、画面内容与摘要用占位长度）估算输入；只发文字。"""
    from ..llm import openai_compat, schemas
    from . import outline

    model = llm_settings.model_for("outline")
    effort = llm_settings.effort_for("outline")
    reasoning = OUTLINE_REASONING.get(effort, OUTLINE_REASONING["medium"])
    segments = _load_segments(video_id, keyframes, cues)
    ev = placeholder_evidence(segments, config.settings.slides.max_speech_chars)
    system, messages = outline.build_request(ev, placeholder_summaries=True)
    text, _ = tokens.request_input_tokens(messages, system, model)
    try:  # 严格模式下 JSON Schema 也计入输入
        text += tokens.text_tokens(json.dumps(openai_compat.strict_json_schema(schemas.OutlineResponse)))
    except Exception as e:  # noqa: BLE001
        log.warning("估算大纲 schema 的 token 失败：%s", e)
    n_sec = len(ev["sections"])
    n_page = sum(1 for s in ev["sections"] if s.get("page") is not None)
    out = (n_sec * OUTLINE_OUTPUT_PER_SECTION[0] + reasoning[0],
           n_sec * OUTLINE_OUTPUT_PER_SECTION[1] + reasoning[1])
    return {"model": model, "effort": effort, "requests": [(text, 0)], "output_range": out,
            "desc": (f"短索引：{n_page} 个幻灯片出现、{n_sec - n_page} 个无幻灯片时段、{len(ev['speech'])} 段讲解"
                     f"（约 {text:,} token，读图结果按固定长度估计），只发文字")}


def _priced(stage: str, est: dict, plan: dict) -> dict:
    """按该用途当前设置的处理档位计价（接口不是 OpenAI 官方时 flex 不生效，按标准价）。

    flex 时 cost_usd_range 按 Flex 价：资源不足改用标准档只在 flex 退避约 1 分钟仍失败时发生，属于例外，
    把它算进上限会让上限翻倍、区间失去参考意义。最坏情况（全部请求都按标准价、取输出上限）另给
    fallback_cost_usd_max，并在说明里注明。"""
    from ..llm import openai_compat

    model = est["model"]
    reqs_lo = est["requests"]
    reqs_hi = est.get("requests_hi", reqs_lo)
    in_lo = sum(t + i for t, i in reqs_lo)
    in_hi = sum(t + i for t, i in reqs_hi)
    out_lo, out_hi = est["output_range"]
    tier = openai_compat.effective_service_tier(stage)
    flex = tier == "flex"
    item = {"stage": stage, "label": STAGE_LABELS[stage], "effort": est.get("effort"),
            "provider": f"{'DeepSeek' if llm_settings.uses_deepseek(stage) else 'OpenAI 兼容'}（{model}，思考 {est.get('effort')}）",
            "model": model, "service_tier": tier,
            "skipped": False, "desc": est["desc"], "input_tokens_range": [in_lo, in_hi],
            "image_tokens": est.get("image_tokens", 0), "output_tokens_range": [out_lo, out_hi]}
    if llm_settings.uses_deepseek(stage):
        item["desc"] += "；DeepSeek 按峰时单价估算（非峰时半价），图片按每张 1024 token 上限、文字与输出按近似值估算；实际以服务商账单为准"
    if not flex and llm_settings.service_tier_for(stage) == "flex":
        item["desc"] += "；当前接口不是 OpenAI 官方，Flex 档不生效，按标准价估算"
    if llm_settings.price_for(model) is None:
        plan["warnings"].append(f"价格表里没有模型 {model}，无法估算费用，可在设置页补充价格")
        return {**item, "estimated_cost_usd": None, "cost_usd_range": None}
    # 输出按请求数均摊，逐个请求计价（单次请求超过长上下文阈值时按长上下文价格）
    def total(reqs, out_total, service_tier):
        per_out = out_total / max(1, len(reqs))
        return sum(tokens.cost_usd(model, t + i, round(per_out), service_tier=service_tier) for t, i in reqs)
    lo, hi = total(reqs_lo, out_lo, tier), total(reqs_hi, out_hi, tier)
    if flex:
        worst = total(reqs_hi, out_hi, "standard")
        item["fallback_cost_usd_max"] = round(worst, 4)
        if not tokens.has_flex_price(model):
            plan["warnings"].append(f"价格表里 {model} 没有 Flex 价，{STAGE_LABELS[stage]}按标准价估算")
    return {**item, "estimated_cost_usd": round((lo + hi) / 2, 4), "cost_usd_range": [round(lo, 4), round(hi, 4)]}


def build_send_plan(video_id: str) -> dict:
    s = config.settings
    cues = _read(video_id, "cues.json")
    keyframes = _read(video_id, "keyframes.json")
    plan: dict = {"items": [], "warnings": [], "blocking": [], "deepl_usage": None}

    t_item = _translate_item(cues, plan)
    plan["items"].append(t_item)

    # 读幻灯片与生成大纲：未开启时只列出；开启时按实际要发送的内容估算 token 与费用
    required_keys = set()
    for stage in ("read_slides", "outline"):
        if runner.is_disabled(video_id, stage):
            plan["items"].append({"stage": stage, "label": STAGE_LABELS[stage], "provider": "—",
                                  "desc": "未开启大纲生成", "estimated_cost_usd": None, "skipped": True})
            continue
        if llm_settings.uses_deepseek(stage):
            required_keys.add("deepseek")
        elif config.settings.llm_provider == "openai":
            required_keys.add("openai")
        if not keyframes and (stage == "read_slides" or not cues):
            plan["items"].append({"stage": stage, "label": STAGE_LABELS[stage], "provider": "—",
                                  "desc": "没有可发送的内容", "estimated_cost_usd": 0.0, "skipped": True})
            continue
        try:
            est = (_read_slides_estimate(video_id, keyframes, cues) if stage == "read_slides"
                   else _outline_estimate(video_id, keyframes, cues))
        except Exception as e:  # noqa: BLE001  估算失败不影响发送，只是无法显示费用
            log.warning("估算 %s 的 token 失败：%s", stage, e)
            plan["warnings"].append(f"{STAGE_LABELS[stage]}的费用无法估算：{e}")
            plan["items"].append({"stage": stage, "label": STAGE_LABELS[stage], "skipped": False,
                                  "provider": f"{'DeepSeek' if llm_settings.uses_deepseek(stage) else 'OpenAI 兼容'}（{llm_settings.model_for(stage)}）",
                                  "desc": "无法估算", "estimated_cost_usd": None, "cost_usd_range": None})
            continue
        plan["items"].append(_priced(stage, est, plan))
    for service in sorted(required_keys):
        if not keys.get_key(service):
            plan["blocking"].append(f"未填写 {'DeepSeek' if service == 'deepseek' else 'OpenAI'} API Key，请到设置页填写")

    fx = llm_settings.get()["usd_to_sgd"]
    lo = sum((i.get("cost_usd_range") or [i["estimated_cost_usd"] or 0] * 2)[0] for i in plan["items"] if not i["skipped"])
    hi = sum((i.get("cost_usd_range") or [i["estimated_cost_usd"] or 0] * 2)[1] for i in plan["items"] if not i["skipped"])
    costs = [i["estimated_cost_usd"] for i in plan["items"] if not i["skipped"]]
    # 最坏情况：flex 阶段全部改用标准档（按标准价、取输出上限），其余阶段取区间上限
    worst = sum(i["fallback_cost_usd_max"] if i.get("fallback_cost_usd_max") is not None
                else (i.get("cost_usd_range") or [i["estimated_cost_usd"] or 0] * 2)[1]
                for i in plan["items"] if not i["skipped"])
    plan.update(
        cost_complete=all(c is not None for c in costs),
        cost_usd_range=[round(lo, 4), round(hi, 4)],
        cost_sgd_range=[round(lo * fx, 2), round(hi * fx, 2)],
        fallback_cost_usd_max=round(worst, 4),
        fallback_cost_sgd_max=round(worst * fx, 2),
        usd_to_sgd=fx,
        prices_as_of=llm_settings.PRICES_AS_OF,
        flex_prices_as_of=llm_settings.FLEX_PRICES_AS_OF,
        estimated_cost_usd=round(sum(c for c in costs if c), 4),
        text_chars=sum(len(c.get("text") or "") for c in cues),
        slide_count=len(keyframes),
        # 旧字段，兼容已有前端
        provider=t_item["provider"],
        translator="Ollama" if s.translation_provider() == 'ollama' else "Google" if s.translation_provider() == 'google' else "DeepL" if "DeepL" in t_item["provider"] else "LLM",
    )
    return plan
