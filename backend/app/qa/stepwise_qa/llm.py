"""Model client owned by the teaching agent; independent of global Q&A keys/settings."""
from __future__ import annotations

import json
import logging
import threading
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from ... import db
from ...llm import client as shared_errors
from ...llm import openai_compat
from . import settings

log = logging.getLogger(__name__)
LLMError = shared_errors.LLMError
LLMTruncatedError = shared_errors.LLMTruncatedError
LLMRefusalError = shared_errors.LLMRefusalError
M = TypeVar("M", bound=BaseModel)
_client = None
_client_signature: tuple[str, str] | None = None
_client_lock = threading.Lock()


def _get_client():
    global _client, _client_signature
    config = settings.get()
    key = settings.api_key()
    signature = (key, config["base_url"])
    with _client_lock:
        if _client is None or signature != _client_signature:
            import openai
            _client = openai.OpenAI(api_key=key, base_url=config["base_url"], timeout=180, max_retries=2)
            _client_signature = signature
    return _client, config


def _record(response, model: str, video_id: str | None, stage: str) -> None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    config = settings.get()
    try:
        db.record_usage(video_id=video_id, stage=f"stepwise_qa:{stage}",
                        provider=f"Stepwise QA Agent ({config['provider']})", model=model,
                        input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                        output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
                        cache_read_tokens=int(getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0))
    except Exception:  # usage tracking must not fail a teaching turn
        log.exception("记录步进式问答 Agent 用量失败")


def _params(model: str, max_tokens: int, effort: str | None, json_mode: bool) -> dict:
    config = settings.get()
    request = {"model": model, "max_tokens": max_tokens}
    if config["provider"] == "deepseek":
        level = effort or config["effort"]
        if level == "none":
            request["extra_body"] = {"thinking": {"type": "disabled"}}
        else:
            level = {"medium": "high", "xhigh": "max"}.get(level, level)
            request["extra_body"] = {"thinking": {"type": "enabled"}}
            request["reasoning_effort"] = level
    elif openai_compat.is_reasoning_model(model):
        request.pop("max_tokens")
        request["max_completion_tokens"] = max_tokens
        if effort and effort != "none":
            request["reasoning_effort"] = effort
    if json_mode:
        request["response_format"] = {"type": "json_object"}
    return request


def complete(*, messages: list[dict], system: str | list | None = None, max_tokens: int = 4096,
             video_id: str | None = None, stage: str = "qa", effort: str | None = None,
             cache_system: bool = False, json_mode: bool = False) -> str:
    client, config = _get_client()
    model = config["model"]
    try:
        response = client.chat.completions.create(
            **_params(model, max_tokens, effort, json_mode),
            messages=openai_compat.convert_messages(messages, system),
        )
    except Exception as error:  # normalize provider-specific failures for the chat adapter
        _raise_provider_error(error)
    _record(response, model, video_id, stage)
    choice = response.choices[0]
    if choice.finish_reason == "length":
        raise LLMTruncatedError("模型输出被截断（推理占满了输出上限），请在步进式问答 Agent 设置中关闭思考或提高输出上限后重试")
    if choice.finish_reason == "content_filter":
        raise LLMRefusalError("模型拒绝了该请求")
    return choice.message.content or ""


def complete_structured(*, messages: list[dict], response_model: type[M], system: str | list | None = None,
                        max_tokens: int = 4096, video_id: str | None = None, stage: str = "qa",
                        effort: str | None = None, cache_system: bool = False, retries: int = 1,
                        **_ignored) -> M:
    schema_text = json.dumps(response_model.model_json_schema(), ensure_ascii=False, indent=2)
    schema_instruction = (
        "返回内容必须是符合以下 JSON Schema 的单个 JSON 对象。所有 required 字段都必须提供；"
        "不要省略字段，不要添加 Markdown 代码围栏。\nJSON Schema:\n" + schema_text
    )
    if system is None:
        structured_system: str | list = schema_instruction
    elif isinstance(system, str):
        structured_system = system + "\n\n" + schema_instruction
    else:
        structured_system = [*system, {"type": "text", "text": schema_instruction}]
    convo = list(messages)
    for attempt in range(retries + 1):
        text = complete(messages=convo, system=structured_system, max_tokens=max_tokens, video_id=video_id,
                        stage=stage, effort=effort, json_mode=True)
        try:
            return response_model.model_validate_json(text)
        except (ValidationError, ValueError, json.JSONDecodeError) as error:
            if attempt >= retries:
                raise LLMError(f"步进式问答 Agent 返回内容无法解析为要求的结构化数据：{error}") from error
            convo = convo + [{"role": "assistant", "content": text},
                             {"role": "user", "content": (
                                 f"上一次结果未通过 Pydantic 字段校验：{error}\n"
                                 "请修正并重试，逐项满足系统中的 JSON Schema，特别是补齐所有 required 字段。"
                             )}]
    raise AssertionError("unreachable")


def _raise_provider_error(error: Exception) -> None:
    import openai
    if isinstance(error, openai.AuthenticationError):
        raise LLMError("步进式问答 Agent API Key 无效或已失效") from error
    if isinstance(error, openai.RateLimitError):
        raise LLMError("步进式问答 Agent 接口限流或账户额度不足") from error
    if isinstance(error, openai.APIConnectionError):
        raise LLMError("无法连接步进式问答 Agent 模型接口，请检查网络和接口地址") from error
    if isinstance(error, openai.APIStatusError):
        raise LLMError(f"步进式问答 Agent 模型接口错误（HTTP {error.status_code}）：{error.message}") from error
    raise LLMError(f"步进式问答 Agent 调用失败：{error}") from error
