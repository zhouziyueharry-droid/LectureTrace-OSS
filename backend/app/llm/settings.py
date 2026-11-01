"""大模型设置：各用途用哪个模型、价格表、汇率、是否自动生成大纲。保存在本机 SQLite（app_settings）。"""
from __future__ import annotations

from .. import db

PURPOSES = ("read_slides", "outline", "qa")
PURPOSE_LABELS = {"read_slides": "读取幻灯片", "outline": "生成大纲", "qa": "问答"}
DEFAULT_MODEL = "gpt-5.6-sol"
DEEPSEEK_MODEL = "deepseek-flash"
DEEPSEEK_PURPOSES = ("read_slides", "outline")

# 思考（推理）强度：GPT-5.6 支持 none / low / medium / high / xhigh / max，推理 token 按输出计费。
# 读幻灯片是转录与概括，低强度足够；大纲与问答需要综合整节课，默认中等
EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")
DEFAULT_EFFORTS = {"read_slides": "low", "outline": "medium", "qa": "medium"}

# 处理档位（service_tier）：flex 半价但更慢，容量不足时返回 429 Resource Unavailable（不收费），
# 由 openai_compat 退避重试后改用标准档（见 openai_compat.FLEX_*）。只对 OpenAI 官方接口生效，DeepSeek 等兼容接口一律标准。
# 读幻灯片与大纲是后台批处理，默认 flex；问答要实时，默认标准。
SERVICE_TIERS = ("flex", "standard")
DEFAULT_SERVICE_TIERS = {"read_slides": "flex", "outline": "flex", "qa": "standard"}

# 官方价格（美元 / 百万 token），来源：https://developers.openai.com/api/docs/pricing ，标准档 2026-09-26 查询。
# 单次请求输入超过 long_threshold 时整次按 long_* 计价；gpt-5.6-sol 为促销价，至少持续到 2026-11-21。
# 写入提示缓存按 cache_write_multiplier × 输入价计费（官方文档 Prompt caching）。
# flex_* 为 Flex 档价格（同页「Flex pricing data」表，2026-09-28 查询）：各项都是标准价的一半，与 Batch 档相同；
# 缓存写入同样按 cache_write_multiplier × flex 输入价（Sol $2.50、Luna $0.125，与表中一致）。
# 没有 flex_* 的模型（例如只填了标准价的自定义价格）实际档位为 flex 时仍按标准价计，宁可高估。
PRICES_AS_OF = "2026-09-26"
FLEX_PRICES_AS_OF = "2026-09-28"
DEFAULT_PRICES: dict[str, dict] = {
    # https://api-docs.deepseek.com/quick_start/pricing/，2026-10-18，按峰时价保守估算。
    "deepseek-flash": {"input": 0.30, "cached_input": 0.006, "output": 1.20,
                       "note": "2026-10-18 峰时价估算；非峰时半价，实际以 DeepSeek 账单为准"},
    # gpt-6 系列：官方价格页 2026-09-28 查询，标准档与 flex 档各为 gpt-5.6 同档的一半；写缓存 = 1.25 × 输入价。
    # 长上下文（>272K）价格未收录：本项目单次输入远小于该阈值，缺省时一律按短上下文计价。
    "gpt-6-sol": {"input": 2.00, "cached_input": 0.20, "output": 10.00, "cache_write_multiplier": 1.25,
                  "flex_input": 1.00, "flex_cached_input": 0.10, "flex_output": 5.00},
    "gpt-6-luna": {"input": 0.10, "cached_input": 0.01, "output": 0.50, "cache_write_multiplier": 1.25,
                   "flex_input": 0.05, "flex_cached_input": 0.005, "flex_output": 0.25},
    "gpt-5.6-sol": {"input": 4.00, "cached_input": 0.40, "output": 20.00,
                    "cache_write_multiplier": 1.25, "long_threshold": 272_000, "long_input": 8.00, "long_cached_input": 0.80, "long_output": 30.00,
                    "flex_input": 2.00, "flex_cached_input": 0.20, "flex_output": 10.00,
                    "flex_long_input": 4.00, "flex_long_cached_input": 0.40, "flex_long_output": 15.00,
                    "note": "促销价，至少持续到 2026-11-21"},
    "gpt-5.6-terra": {"input": 2.00, "cached_input": 0.20, "output": 12.00,
                      "cache_write_multiplier": 1.25, "long_threshold": 272_000, "long_input": 4.00, "long_cached_input": 0.40, "long_output": 18.00,
                      "flex_input": 1.00, "flex_cached_input": 0.10, "flex_output": 6.00,
                      "flex_long_input": 2.00, "flex_long_cached_input": 0.20, "flex_long_output": 9.00},
    "gpt-5.6-luna": {"input": 0.20, "cached_input": 0.02, "output": 1.20,
                     "cache_write_multiplier": 1.25, "long_threshold": 272_000, "long_input": 0.40, "long_cached_input": 0.04, "long_output": 1.80,
                     "flex_input": 0.10, "flex_cached_input": 0.01, "flex_output": 0.60,
                     "flex_long_input": 0.20, "flex_long_cached_input": 0.02, "flex_long_output": 0.90},
    "gpt-5.5": {"input": 5.00, "cached_input": 0.50, "output": 30.00,
                "long_threshold": 272_000, "long_input": 10.00, "long_cached_input": 1.00, "long_output": 45.00,
                "flex_input": 2.50, "flex_cached_input": 0.25, "flex_output": 15.00,
                "flex_long_input": 5.00, "flex_long_cached_input": 0.50, "flex_long_output": 22.50},
}
# 美元兑新加坡元，2026-09 参考汇率，可在设置页修改
DEFAULT_USD_TO_SGD = 1.2778

# 图片按 32×32 像素切块计 token（官方文档 Images and vision），GPT-5.x 倍率 1.2
IMAGE_TOKEN_MULTIPLIER = {"gpt-5": 1.2, "gpt-6": 1.2}  # gpt-6 文档只列了 astra 为 1.2，同代按 1.2 估算


# 大纲与问答的输出语言（交接 0001 §10.5）；中间产物一律用课程原语言，不受此设置影响
OUTPUT_LANGUAGES = {"zh-CN": "简体中文", "en": "English"}
DEFAULT_OUTPUT_LANGUAGE = "zh-CN"


def _get() -> dict:
    return db.get_setting("llm_settings", {}) or {}


def get() -> dict:
    s = _get()
    models = {p: (s.get("models") or {}).get(p) or DEFAULT_MODEL for p in PURPOSES}
    efforts = {p: (s.get("efforts") or {}).get(p) or DEFAULT_EFFORTS[p] for p in PURPOSES}
    saved_tiers = s.get("service_tiers") or {}
    tiers = {p: saved_tiers.get(p) if saved_tiers.get(p) in SERVICE_TIERS else DEFAULT_SERVICE_TIERS[p]
             for p in PURPOSES}
    return {
        "models": models,
        "efforts": efforts,
        "effort_options": list(EFFORTS),
        "service_tiers": tiers,
        "service_tier_options": list(SERVICE_TIERS),
        "outline_enabled": bool(s.get("outline_enabled", False)),
        "output_language": s.get("output_language") if s.get("output_language") in OUTPUT_LANGUAGES
        else DEFAULT_OUTPUT_LANGUAGE,
        "output_language_options": OUTPUT_LANGUAGES,
        "usd_to_sgd": float(s.get("usd_to_sgd") or DEFAULT_USD_TO_SGD),
        "prices": {**DEFAULT_PRICES, **(s.get("prices") or {})},
        "custom_prices": sorted((s.get("prices") or {}).keys()),
        "prices_as_of": PRICES_AS_OF,
        "flex_prices_as_of": FLEX_PRICES_AS_OF,
    }


def update(values: dict) -> dict:
    """values 可含 models（部分用途）、efforts、service_tiers（部分用途，flex | standard）、outline_enabled、usd_to_sgd、output_language、prices（按模型覆盖；值为 null 表示恢复默认）。"""
    s = _get()
    if "models" in values and values["models"] is not None:
        if any(k not in DEEPSEEK_PURPOSES and isinstance(v, str) and v.strip() == DEEPSEEK_MODEL
               for k, v in values["models"].items()):
            raise ValueError("DeepSeek Flash 仅用于读取幻灯片和生成大纲，问答与翻译保留原接口")
        s["models"] = {**(s.get("models") or {}), **{k: v.strip() for k, v in values["models"].items()
                                                      if k in PURPOSES and isinstance(v, str) and v.strip()}}
    if values.get("efforts") is not None:
        bad = [v for v in values["efforts"].values() if v not in EFFORTS]
        if bad:
            raise ValueError(f"未知的思考强度：{', '.join(bad)}")
        s["efforts"] = {**(s.get("efforts") or {}), **{k: v for k, v in values["efforts"].items() if k in PURPOSES}}
    if values.get("service_tiers") is not None:
        bad = [str(v) for v in values["service_tiers"].values() if v not in SERVICE_TIERS]
        if bad:
            raise ValueError(f"未知的处理档位：{', '.join(bad)}（可选 {' / '.join(SERVICE_TIERS)}）")
        s["service_tiers"] = {**(s.get("service_tiers") or {}),
                              **{k: v for k, v in values["service_tiers"].items() if k in PURPOSES}}
    if values.get("outline_enabled") is not None:
        s["outline_enabled"] = bool(values["outline_enabled"])
    if values.get("usd_to_sgd") is not None:
        s["usd_to_sgd"] = float(values["usd_to_sgd"])
    if values.get("output_language") is not None:
        if values["output_language"] not in OUTPUT_LANGUAGES:
            raise ValueError(f"不支持的输出语言：{values['output_language']}")
        s["output_language"] = values["output_language"]
    if values.get("prices") is not None:
        prices = dict(s.get("prices") or {})
        for model, p in values["prices"].items():
            if p is None:
                prices.pop(model, None)
            else:
                prices[model] = {k: float(v) for k, v in p.items() if isinstance(v, (int, float))}
        s["prices"] = prices
    db.set_setting("llm_settings", s)
    return get()


def model_for(purpose: str) -> str:
    return get()["models"].get(purpose) or DEFAULT_MODEL


def uses_deepseek(purpose: str) -> bool:
    return purpose in DEEPSEEK_PURPOSES and model_for(purpose) == DEEPSEEK_MODEL


def effort_for(purpose: str) -> str:
    return get()["efforts"].get(purpose) or DEFAULT_EFFORTS.get(purpose, "medium")


def service_tier_for(purpose: str) -> str:
    """该用途设置的处理档位（flex | standard）。不在 PURPOSES 里的 stage（如 LLM 翻译）与选模型一样按 qa 处理。
    这是设置值；实际是否发送 flex 还要看接口是否为 OpenAI 官方（openai_compat.effective_service_tier）。"""
    p = purpose if purpose in PURPOSES else "qa"
    return get()["service_tiers"].get(p) or DEFAULT_SERVICE_TIERS[p]


def price_for(model: str) -> dict | None:
    return get()["prices"].get(model)


def image_multiplier(model: str) -> float:
    for prefix, m in IMAGE_TOKEN_MULTIPLIER.items():
        if model.startswith(prefix):
            return m
    return 1.0


def outline_enabled_for(video_id: str) -> bool:
    """全局开关，或该视频被单独要求生成大纲。"""
    return get()["outline_enabled"] or bool(db.get_setting(f"outline_run:{video_id}", False))
