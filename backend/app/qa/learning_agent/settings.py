"""Independent model settings for the teaching agent."""
from __future__ import annotations

from ... import db

SETTING_KEY = "learning_agent_settings"
PROVIDERS = {
    "deepseek": "https://api.deepseek.com",
    "openai": "https://api.openai.com/v1",
    "custom": "https://api.openai.com/v1",
}
EFFORTS = ("none", "low", "medium", "high", "max")
DEFAULTS = {
    "provider": "deepseek",
    "base_url": PROVIDERS["deepseek"],
    "model": "deepseek-v4-pro",
    "effort": "none",
    "max_output_tokens": 16000,
    "output_language": "zh-CN",
}


def _stored() -> dict:
    return db.get_setting(SETTING_KEY, {}) or {}


def get() -> dict:
    stored = _stored()
    values = {**DEFAULTS, **{k: v for k, v in stored.items() if k in DEFAULTS}}
    api_key = str(stored.get("api_key") or "").strip()
    values.update(has_key=bool(api_key), key_hint=f"…{api_key[-4:]}" if api_key else None)
    return values


def update(values: dict) -> dict:
    stored = _stored()
    provider = values.get("provider", stored.get("provider", DEFAULTS["provider"]))
    if provider not in PROVIDERS:
        raise ValueError("服务商必须是 DeepSeek、OpenAI 或自定义兼容接口")
    model = str(values.get("model", stored.get("model", DEFAULTS["model"]))).strip()
    if not model:
        raise ValueError("模型 ID 不能为空")
    effort = values.get("effort", stored.get("effort", DEFAULTS["effort"]))
    if effort not in EFFORTS:
        raise ValueError("不支持的思考强度")
    try:
        max_output_tokens = int(values.get("max_output_tokens", stored.get("max_output_tokens", DEFAULTS["max_output_tokens"])))
    except (TypeError, ValueError):
        raise ValueError("输出上限必须是整数") from None
    if not 1000 <= max_output_tokens <= 64000:
        raise ValueError("输出上限须在 1000 到 64000 tokens 之间")
    base_url = str(values.get("base_url") or stored.get("base_url") or PROVIDERS[provider]).strip().rstrip("/")
    if not base_url.startswith(("https://", "http://")):
        raise ValueError("接口地址必须以 http:// 或 https:// 开头")

    result = {**stored, "provider": provider, "base_url": base_url, "model": model,
              "effort": effort, "max_output_tokens": max_output_tokens,
              "output_language": values.get("output_language", stored.get("output_language", DEFAULTS["output_language"]))}
    new_key = str(values.get("api_key") or "").strip()
    if new_key:
        result["api_key"] = new_key
    if values.get("clear_api_key"):
        result.pop("api_key", None)
    if result["output_language"] not in ("zh-CN", "en"):
        raise ValueError("输出语言只能选择简体中文或 English")
    db.set_setting(SETTING_KEY, result)
    return get()


def api_key() -> str:
    key = str(_stored().get("api_key") or "").strip()
    if not key:
        raise ValueError("请先在教学 Agent 设置中填写 API Key")
    return key


def effort() -> str:
    return str(get()["effort"])
