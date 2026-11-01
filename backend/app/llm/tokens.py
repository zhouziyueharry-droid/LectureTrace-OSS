"""发送前的 token 计数与费用估算：文字用 tiktoken（o200k_base），图片按官方 32×32 切块公式。"""
from __future__ import annotations

import base64
import io
import logging
import math

from PIL import Image

from . import settings as llm_settings

log = logging.getLogger(__name__)

PATCH = 32
HIGH_PATCH_BUDGET = 2500          # detail=high：最多 2500 块，且长边不超过 2048
HIGH_MAX_SIDE = 2048
MESSAGE_OVERHEAD = 4              # 每条消息的格式开销（估计值）

_enc = None


def _encoding():
    global _enc
    if _enc is None:
        try:
            import tiktoken

            _enc = tiktoken.get_encoding("o200k_base")
        except Exception as e:  # noqa: BLE001  没有网络下载编码表时退回按字符估算
            log.warning("tiktoken 不可用，按字符数估算 token：%s", e)
            _enc = False
    return _enc


def text_tokens(text: str) -> int:
    enc = _encoding()
    if enc:
        return len(enc.encode(text, disallowed_special=()))
    return math.ceil(len(text) / 3.5)


def image_tokens(width: int, height: int, model: str) -> int:
    """官方公式（detail=high）：按 32 像素切块；超过 2500 块或长边超过 2048 时等比缩小；再乘模型倍率。"""
    if model == "deepseek-flash":
        # shortcut: 按官方每图上限保守估算，需精确预算时接入 DeepSeek 图片计数公式。
        return 1024
    scale = min(1.0, HIGH_MAX_SIDE / max(width, height))
    w, h = width * scale, height * scale
    if math.ceil(w / PATCH) * math.ceil(h / PATCH) > HIGH_PATCH_BUDGET:
        shrink = math.sqrt(PATCH * PATCH * HIGH_PATCH_BUDGET / (w * h))
        shrink *= min(math.floor(w * shrink / PATCH) / (w * shrink / PATCH),
                      math.floor(h * shrink / PATCH) / (h * shrink / PATCH))
        w, h = w * shrink, h * shrink
    patches = math.ceil(w / PATCH) * math.ceil(h / PATCH)
    return math.ceil(patches * llm_settings.image_multiplier(model))


def image_block_tokens(block: dict, model: str) -> int:
    data = base64.b64decode(block["source"]["data"])
    with Image.open(io.BytesIO(data)) as im:
        return image_tokens(im.width, im.height, model)


def request_input_tokens(messages: list[dict], system: str | list | None, model: str) -> tuple[int, int]:
    """返回 (文字 token, 图片 token)。messages 为本项目统一的 Anthropic 风格内容块。"""
    text = img = 0
    if system:
        sys_text = system if isinstance(system, str) else "".join(b.get("text", "") for b in system)
        text += text_tokens(sys_text) + MESSAGE_OVERHEAD
    for m in messages:
        text += MESSAGE_OVERHEAD
        content = m["content"]
        if isinstance(content, str):
            text += text_tokens(content)
            continue
        for b in content:
            if b.get("type") == "image":
                img += image_block_tokens(b, model)
            else:
                text += text_tokens(b.get("text", ""))
    return text, img


def is_flex_tier(service_tier: str | None) -> bool:
    """响应里的 service_tier（或设置里的档位）是否按 Flex 价计费：只有 "flex" 算，其余（default / auto / standard /
    None 等）都按标准价。"""
    return (service_tier or "").strip().lower() == "flex"


def _flex_view(p: dict) -> dict:
    """把价格表条目换成 Flex 价：有 flex_* 的项替换对应的标准价，没有的保持标准价（宁可高估）。"""
    out = dict(p)
    for k in ("input", "cached_input", "output", "long_input", "long_cached_input", "long_output"):
        v = p.get(f"flex_{k}")
        if isinstance(v, (int, float)):
            out[k] = v
    return out


def has_flex_price(model: str) -> bool:
    p = llm_settings.price_for(model) or {}
    return all(isinstance(p.get(f"flex_{k}"), (int, float)) for k in ("input", "output"))


def cost_usd(model: str, input_tokens: int, output_tokens: int, cached_tokens: int = 0,
             cache_write_tokens: int = 0, service_tier: str | None = None) -> float | None:
    """单次请求的费用；价格表里没有该模型时返回 None。

    input_tokens 为全部输入：其中命中缓存的按缓存价，写入缓存的按 cache_write_multiplier × 输入价，其余按输入价。
    service_tier：实际处理档位（响应里的 service_tier）。"flex" 按价格表的 flex_* 计价（缺的项退回标准价），
    其余一律按标准价。
    """
    p = llm_settings.price_for(model)
    if not p:
        return None
    if is_flex_tier(service_tier):
        p = _flex_view(p)
    long = input_tokens > p.get("long_threshold", float("inf"))
    pin = p.get("long_input", p["input"]) if long else p["input"]
    pcache = p.get("long_cached_input", p.get("cached_input", pin)) if long else p.get("cached_input", pin)
    pout = p.get("long_output", p["output"]) if long else p["output"]
    pwrite = pin * p.get("cache_write_multiplier", 1.0)
    plain = input_tokens - cached_tokens - cache_write_tokens
    return (plain * pin + cached_tokens * pcache + cache_write_tokens * pwrite + output_tokens * pout) / 1_000_000
