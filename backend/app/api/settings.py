"""设置页接口：API Key 管理。Key 只保存在本机后端，返回给前端的只有打码提示。"""
from __future__ import annotations

import time
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import config, keys, db
from ..llm import settings as llm_settings

router = APIRouter(prefix="/api/settings", tags=["settings"])


class KeysUpdate(BaseModel):
    # None 表示不修改，"" 表示清除
    deepl: str | None = None
    google: str | None = None
    translator: Literal['deepl', 'google', 'llm', 'ollama'] | None = None
    openai: str | None = None
    deepseek: str | None = None
    openai_base_url: str | None = None


class TestBody(BaseModel):
    service: str


class LlmUpdate(BaseModel):
    models: dict[str, str] | None = None            # 用途 → 模型 ID
    efforts: dict[str, str] | None = None           # 用途 → 思考强度
    service_tiers: dict[str, str] | None = None     # 用途 → 处理档位：flex | standard
    outline_enabled: bool | None = None
    usd_to_sgd: float | None = None
    output_language: str | None = None               # 大纲与问答的输出语言：zh-CN | en
    prices: dict[str, dict | None] | None = None     # 模型 → 价格；null 表示恢复默认


def _view() -> dict:
    return {"keys": keys.public_view(),
            "translator": config.settings.translation_provider(),
            "ollama": {"model": config.settings.ollama_translate.model, "base_url": "http://127.0.0.1:11434"},
            "capabilities": {"asr": {"enabled": config.settings.asr.enabled,
                                     "model": config.settings.asr.model,
                                     "device": config.settings.asr.device}},
            "llm": {**llm_settings.get(), "provider": config.settings.llm_provider,
                    "purposes": [{"id": p, "label": llm_settings.PURPOSE_LABELS[p]} for p in llm_settings.PURPOSES],
                    "default_prices": llm_settings.DEFAULT_PRICES}}


@router.get("")
def get_settings():
    return _view()


@router.put("")
def put_settings(body: KeysUpdate):
    base = body.openai_base_url
    if base and not base.strip().startswith(("http://", "https://")):
        raise HTTPException(400, "接口地址需要以 http:// 或 https:// 开头")
    if body.translator is not None:
        if db.fetch_one("SELECT id FROM videos WHERE status='processing' LIMIT 1"):
            raise HTTPException(409, '请等待课程处理结束，再切换翻译服务')
        if body.translator != config.settings.translation_provider():
            # 原服务的发送同意不用于新服务；保留译文和失败状态，重试时重新确认。
            db.execute("UPDATE stages SET status='pending', error=NULL WHERE name='await_confirm' "
                       "AND video_id IN (SELECT video_id FROM stages WHERE name='translate' "
                       "AND status NOT IN ('done','skipped'))")
        db.set_setting('translation_provider', body.translator)
    keys.update(body.model_dump(exclude={'translator'}))
    return _view()


@router.put("/llm")
def put_llm_settings(body: LlmUpdate):
    if body.usd_to_sgd is not None and not 0.1 < body.usd_to_sgd < 100:
        raise HTTPException(400, "汇率不合理")
    for model, p in (body.prices or {}).items():
        if p is not None and not all(isinstance(p.get(k), (int, float)) and p[k] >= 0
                                     for k in ("input", "output")):
            raise HTTPException(400, f"{model} 的价格需要填写输入与输出单价（美元 / 百万 token）")
    try:
        llm_settings.update(body.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _view()


@router.get("/models")
def list_models():
    """用已保存的 Key 向 OpenAI 兼容接口请求可用模型列表。"""
    key = keys.get_key("openai")
    if not key:
        return {"models": [], "error": "尚未填写 OpenAI API Key"}
    try:
        import openai

        client = openai.OpenAI(api_key=key, base_url=keys.openai_base_url(), timeout=20, max_retries=0)
        ids = sorted({m.id for m in client.models.list()})
    except Exception as e:  # noqa: BLE001
        return {"models": [], "error": _error_text(e)}
    return {"models": ids, "error": None}


@router.post("/test")
def test_service(body: TestBody):
    if body.service == 'ollama':
        from ..pipeline.ollama_translate import check_model
        t0 = time.monotonic()
        try:
            model = check_model()
            return {"ok": True, "message": f'本机 Ollama 可连接，已安装 {model}（未运行翻译）',
                    "latency_ms": int((time.monotonic() - t0) * 1000)}
        except Exception as e:
            return {"ok": False, "message": _error_text(e), "latency_ms": int((time.monotonic() - t0) * 1000)}
    if body.service not in keys.SERVICES:
        raise HTTPException(400, f"未知的服务：{body.service}")
    key = keys.get_key(body.service)
    if not key:
        return {"ok": False, "message": "尚未填写 API Key", "latency_ms": 0}
    t0 = time.monotonic()
    try:
        if body.service == 'deepseek':
            import openai

            client = openai.OpenAI(api_key=key, base_url=keys.DEEPSEEK_BASE_URL, timeout=20, max_retries=0)
            if llm_settings.DEEPSEEK_MODEL not in {m.id for m in client.models.list()}:
                raise ValueError('账户模型列表未包含 deepseek-flash')
            message = '可访问 DeepSeek Flash；仅查询模型列表，未发送课程内容，未验证余额或生成效果'
        elif body.service == 'google':
            from ..pipeline.translate import google_request
            result = google_request(key)
            if not isinstance(result.get('data'), dict) or not result['data'].get('languages'):
                raise ValueError('Google 未返回支持的语言列表')
            message = '可连接 Google Cloud Translation；未发送翻译文本，未校验剩余额度'
        else:
            message = _test_deepl(key) if body.service == "deepl" else _test_openai(key)
        ok = True
    except Exception as e:  # noqa: BLE001
        ok, message = False, _error_text(e)
    return {"ok": ok, "message": message, "latency_ms": int((time.monotonic() - t0) * 1000)}


def _test_deepl(key: str) -> str:
    import deepl

    u = deepl.DeepLClient(key).get_usage()
    plan = "Free" if keys.is_free_key(key) else "Pro"
    if u.character is not None and u.character.valid:
        return f"可用（DeepL API {plan}），本期已用 {u.character.count:,} / {u.character.limit:,} 字符"
    return f"可用（DeepL API {plan}）"


def _test_openai(key: str) -> str:
    import openai

    client = openai.OpenAI(api_key=key, base_url=keys.openai_base_url(), timeout=20, max_retries=0)
    ids = sorted(m.id for m in client.models.list())
    return f"可用，账户可访问 {len(ids)} 个模型"


def _error_text(e: Exception) -> str:
    name = type(e).__name__
    if "Authorization" in name or "Authentication" in name or "403" in str(e) or "401" in str(e):
        return "Key 无效或已失效"
    if "Connection" in name or "Timeout" in name:
        return "无法连接服务，请检查网络或接口地址"
    return f"{name}: {e}"
