"""API Key 管理：保存在本机 SQLite（app_settings），设置页未填写时回退到 .env。前端只拿到打码后的提示。"""
from __future__ import annotations

from . import config, db

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPL_USD_PER_CHAR = 25.0 / 1_000_000  # DeepL API Pro 约 $25 / 百万字符（估算）
GOOGLE_USD_PER_CHAR = 20.0 / 1_000_000  # NMT 标准价，未扣账户免费抵扣（2026-10-17）


def is_free_key(api_key: str) -> bool:
    """DeepL API Free 的 Key 以 ":fx" 结尾。"""
    return api_key.strip().endswith(":fx")


# 服务名 → .env 中的后备值
_ENV_FALLBACK = {
    "deepl": lambda: config.settings.deepl_api_key,
    "google": lambda: config.settings.google_translate_api_key,
    "openai": lambda: config.settings.openai_api_key,
    "deepseek": lambda: config.settings.deepseek_api_key,
}
SERVICES = tuple(_ENV_FALLBACK)


def _stored() -> dict:
    return db.get_setting("api_keys", {}) or {}


def get_key(service: str) -> str:
    return (_stored().get(service) or _ENV_FALLBACK[service]() or "").strip()


def key_source(service: str) -> str | None:
    if _stored().get(service):
        return "settings"
    return "env" if _ENV_FALLBACK[service]() else None


def hint(key: str) -> str | None:
    return f"…{key[-4:]}" if key else None


def openai_base_url() -> str:
    return _stored().get("openai_base_url") or config.settings.openai_base_url or DEFAULT_OPENAI_BASE_URL


def update(values: dict) -> None:
    """values 中 None 表示不修改，"" 表示清除。"""
    stored = _stored()
    for field, value in values.items():
        if value is None:
            continue
        value = value.strip()
        if value:
            stored[field] = value
        else:
            stored.pop(field, None)
    db.set_setting("api_keys", stored)


def public_view() -> dict:
    out = {}
    for s in SERVICES:
        k = get_key(s)
        out[s] = {"has_key": bool(k), "key_hint": hint(k), "source": key_source(s)}
    out["openai"]["base_url"] = openai_base_url()
    return out
