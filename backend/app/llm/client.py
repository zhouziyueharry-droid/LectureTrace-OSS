"""大模型调用层：complete / stream / complete_json / complete_structured，并记录用量与费用。

llm_provider == "openai" 时走 OpenAI 兼容接口（openai_compat.py），否则走下面的 Anthropic 官方 SDK 封装。
OpenAI 分支按用途（stage）取处理档位（settings.service_tier_for：读幻灯片与大纲默认 flex，问答标准）；
flex 容量不足时的退避与改用标准档见 openai_compat.create，计费按响应里实际的 service_tier。
结构化调用（complete_structured）见文件末尾。以下几条说的是 Anthropic 分支：

- 一律走流式接口（messages.stream），长输出也不会触发 HTTP 超时。
- SDK 自带 429/5xx 重试（max_retries）；这里再对流中途的过载/断线做外层重试。
- 每次调用都写 api_usage（db.record_usage）。
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any, Callable, Iterator, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from .. import config, db

log = logging.getLogger(__name__)

PROVIDER = "Anthropic"

# 价格表：美元 / 百万 token，(输入, 输出, 缓存读取)。查不到的模型费用记 0。
PRICES: dict[str, tuple[float, float, float]] = {
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-sonnet-4-6": (3.0, 15.0, 0.30),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-4-8": (5.0, 25.0, 0.50),
    "claude-opus-4-7": (5.0, 25.0, 0.50),
    "claude-opus-4-6": (5.0, 25.0, 0.50),
    "claude-haiku-4-5": (1.0, 5.0, 0.10),
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-fable-5": (10.0, 50.0, 1.00),
}
CACHE_WRITE_RATIO = 1.25  # 5 分钟缓存写入按输入价 1.25 倍计

_OUTER_RETRIES = 3
_RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


class LLMError(Exception):
    """调用大模型失败，message 为给用户看的中文原因。"""


class LLMRefusalError(LLMError):
    """模型拒答（`message.refusal` 非空，或 `finish_reason == "content_filter"`）。"""


class LLMTruncatedError(LLMError):
    """输出达到上限被截断（`finish_reason == "length"`）。"""


class LLMInvalidOutputError(LLMError):
    """重试后输出仍不符合返回模型（或 check 回调）。`raw_text` 为最后一次的原始回复，`problems` 为问题列表。"""

    def __init__(self, message: str, *, raw_text: str = "", problems: list[str] | None = None):
        super().__init__(message)
        self.raw_text = raw_text
        self.problems = problems or []


# ---------- 费用 ----------

def estimate_cost(model: str | None, input_tokens: int = 0, output_tokens: int = 0,
                  cache_read_tokens: int = 0, cache_write_tokens: int = 0) -> float:
    price = PRICES.get(model or "")
    if not price:
        return 0.0
    p_in, p_out, p_cache = price
    return (input_tokens * p_in + output_tokens * p_out + cache_read_tokens * p_cache
            + cache_write_tokens * p_in * CACHE_WRITE_RATIO) / 1_000_000


# ---------- 内部工具 ----------

_client: anthropic.Anthropic | None = None
_client_key: str | None = None
_lock = threading.Lock()


def _get_client() -> anthropic.Anthropic:
    global _client, _client_key
    key = config.settings.anthropic_api_key
    if not key:
        raise LLMError("未配置 ANTHROPIC_API_KEY")
    with _lock:
        if _client is None or _client_key != key:
            _client = anthropic.Anthropic(api_key=key, max_retries=4, timeout=600.0)
            _client_key = key
        return _client


def _build_system(system: str | list | None, cache_system: bool) -> Any:
    if system is None:
        return anthropic.omit
    if isinstance(system, str) and not cache_system:
        return system
    blocks = [{"type": "text", "text": system}] if isinstance(system, str) else [dict(b) for b in system]
    if cache_system and blocks:
        blocks[-1] = {**blocks[-1], "cache_control": {"type": "ephemeral"}}
    return blocks


def _count_images(messages: list[dict]) -> int:
    n = 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            n += sum(1 for b in content if isinstance(b, dict) and b.get("type") == "image")
    return n


def _request(messages: list[dict], system: Any, max_tokens: int, cache_system: bool,
             effort: str | None) -> dict:
    model = config.settings.llm_model
    kwargs: dict[str, Any] = {
        "model": model, "max_tokens": max_tokens, "messages": messages,
        "system": _build_system(system, cache_system),
    }
    # effort 仅部分模型支持；Haiku 不支持，直接忽略
    if effort and not model.startswith("claude-haiku"):
        kwargs["output_config"] = {"effort": effort}
    return kwargs


def _record(msg: Any, *, video_id: str | None, stage: str, model: str, images: int) -> None:
    u = getattr(msg, "usage", None)
    inp = int(getattr(u, "input_tokens", 0) or 0)
    out = int(getattr(u, "output_tokens", 0) or 0)
    cread = int(getattr(u, "cache_read_input_tokens", 0) or 0)
    cwrite = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
    try:
        db.record_usage(video_id=video_id, stage=stage, provider=PROVIDER, model=model,
                        input_tokens=inp + cwrite, output_tokens=out, cache_read_tokens=cread,
                        images=images, cost_usd=estimate_cost(model, inp, out, cread, cwrite))
    except Exception:  # 记账失败不影响主流程
        log.exception("记录 API 用量失败")


def _text_of(msg: Any) -> str:
    return "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text")


def _check_stop(msg: Any) -> None:
    if getattr(msg, "stop_reason", None) == "refusal":
        raise LLMError("模型拒绝了该请求")


def _retryable(e: Exception) -> bool:
    if isinstance(e, (anthropic.APIConnectionError, anthropic.APITimeoutError)):
        return True
    return isinstance(e, anthropic.APIStatusError) and e.status_code in _RETRY_STATUS


def _to_llm_error(e: Exception) -> LLMError:
    if isinstance(e, anthropic.AuthenticationError):
        return LLMError("ANTHROPIC_API_KEY 无效或已失效")
    if isinstance(e, anthropic.RateLimitError):
        return LLMError("模型接口限流，请稍后重试")
    if isinstance(e, anthropic.APIStatusError):
        return LLMError(f"模型接口错误（HTTP {e.status_code}）：{getattr(e, 'message', e)}")
    if isinstance(e, (anthropic.APIConnectionError, anthropic.APITimeoutError)):
        return LLMError("无法连接模型接口，请检查网络")
    return LLMError(f"调用模型失败：{e}")


def _sleep(attempt: int) -> None:
    time.sleep(min(30.0, 2.0 * (2 ** attempt)))


# ---------- OpenAI 兼容接口 ----------

def _use_openai(stage: str = "") -> bool:
    from . import settings as llm_settings

    return config.settings.llm_provider == "openai" or llm_settings.uses_deepseek(stage)


def _openai_model(stage: str) -> str:
    from . import settings as llm_settings

    return llm_settings.model_for(stage if stage in llm_settings.PURPOSES else "qa")


def _record_openai(u, *, video_id: str | None, stage: str, model: str, images: int) -> None:
    """按响应里实际的 service_tier 计价（flex → Flex 价，其余或未返回 → 标准价），并把实际档位记进 api_usage。"""
    from . import tokens

    tier = getattr(u, "service_tier", None)
    cost = tokens.cost_usd(model, u.input_tokens, u.output_tokens, u.cached_tokens, u.cache_write_tokens,
                           service_tier=tier)
    try:
        db.record_usage(video_id=video_id, stage=stage, provider="DeepSeek" if model == "deepseek-flash" else "OpenAI 兼容", model=model,
                        input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                        cache_read_tokens=u.cached_tokens, images=images, cost_usd=cost or 0.0,
                        service_tier=tier)
    except Exception:  # 记账失败不影响主流程
        log.exception("记录 API 用量失败")


def _cache_key(cache_system: bool, video_id: str | None, stage: str) -> str | None:
    """需要反复发送同一前缀的请求（问答）按视频分组缓存；其余请求不写缓存。"""
    return f"lecturetrace-{stage}-{video_id or 'none'}" if cache_system else None


def _openai_service_tier(stage: str) -> str:
    """该用途设置的处理档位（与选模型相同：不在 PURPOSES 里的 stage 按 qa）。是否真的发送 flex 由 build_request 判断。"""
    from . import settings as llm_settings

    return llm_settings.service_tier_for(stage)


def _openai_request(messages, system, max_tokens, stage, effort, json_mode, cache_key=None) -> dict:
    from . import openai_compat

    return openai_compat.build_request(model=_openai_model(stage), messages=messages, system=system,
                                       max_tokens=max_tokens, effort=effort, json_mode=json_mode,
                                       cache_key=cache_key, service_tier=_openai_service_tier(stage))


def _openai_complete(messages, system, max_tokens, video_id, stage, effort, json_mode,
                     cache_system=False) -> str:
    from . import openai_compat

    req = _openai_request(messages, system, max_tokens, stage, effort, json_mode,
                          _cache_key(cache_system, video_id, stage))
    try:
        text, usage = openai_compat.complete(req)
    except Exception as e:  # noqa: BLE001
        raise openai_compat.to_llm_error(e) from e
    _record_openai(usage, video_id=video_id, stage=stage, model=req["model"], images=_count_images(messages))
    return text


def _openai_stream(messages, system, max_tokens, video_id, stage, effort, cache_system=False) -> Iterator[str]:
    from . import openai_compat

    req = _openai_request(messages, system, max_tokens, stage, effort, False,
                          _cache_key(cache_system, video_id, stage))
    record = lambda u: _record_openai(u, video_id=video_id, stage=stage, model=req["model"],  # noqa: E731
                                      images=_count_images(messages))
    try:
        yield from openai_compat.stream(req, record)
    except Exception as e:  # noqa: BLE001
        raise openai_compat.to_llm_error(e) from e


# ---------- 公开接口 ----------

def complete(*, messages: list[dict], system: str | list | None = None, max_tokens: int = 4096,
             video_id: str | None, stage: str, cache_system: bool = False,
             effort: str | None = None, json_mode: bool = False) -> str:
    """一次完整调用，返回文本。"""
    if _use_openai(stage):
        return _openai_complete(messages, system, max_tokens, video_id, stage, effort, json_mode, cache_system)
    client = _get_client()
    kwargs = _request(messages, system, max_tokens, cache_system, effort)
    images = _count_images(messages)
    for attempt in range(_OUTER_RETRIES):
        try:
            with client.messages.stream(**kwargs) as s:
                msg = s.get_final_message()
            break
        except LLMError:
            raise
        except Exception as e:  # noqa: BLE001
            if _retryable(e) and attempt < _OUTER_RETRIES - 1:
                log.warning("LLM 调用失败，重试 %d：%s", attempt + 1, e)
                _sleep(attempt)
                continue
            raise _to_llm_error(e) from e
    _record(msg, video_id=video_id, stage=stage, model=kwargs["model"], images=images)
    _check_stop(msg)
    return _text_of(msg)


def stream(*, messages: list[dict], system: str | list | None = None, max_tokens: int = 4096,
           video_id: str | None, stage: str, cache_system: bool = False,
           effort: str | None = None) -> Iterator[str]:
    """流式产出文本片段；只有尚未产出任何文本时才重试。"""
    if _use_openai(stage):
        yield from _openai_stream(messages, system, max_tokens, video_id, stage, effort, cache_system)
        return
    client = _get_client()
    kwargs = _request(messages, system, max_tokens, cache_system, effort)
    images = _count_images(messages)
    for attempt in range(_OUTER_RETRIES):
        emitted = False
        try:
            with client.messages.stream(**kwargs) as s:
                for text in s.text_stream:
                    emitted = True
                    yield text
                msg = s.get_final_message()
            break
        except LLMError:
            raise
        except Exception as e:  # noqa: BLE001
            if not emitted and _retryable(e) and attempt < _OUTER_RETRIES - 1:
                log.warning("LLM 流式调用失败，重试 %d：%s", attempt + 1, e)
                _sleep(attempt)
                continue
            raise _to_llm_error(e) from e
    _record(msg, video_id=video_id, stage=stage, model=kwargs["model"], images=images)
    _check_stop(msg)


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_json(text: str) -> Any:
    """从模型回复中解析 JSON：支持 ```json 代码块、前后夹杂说明文字。失败抛 ValueError。"""
    candidates = [text.strip()]
    candidates += [m.strip() for m in _FENCE_RE.findall(text)]
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        i, j = text.find(open_ch), text.rfind(close_ch)
        if 0 <= i < j:
            candidates.append(text[i:j + 1])
    for c in candidates:
        try:
            return json.loads(c)
        except (json.JSONDecodeError, ValueError):
            continue
    raise ValueError("模型回复不是有效的 JSON")


def complete_json(*, messages: list[dict], system: str | list | None = None, max_tokens: int = 4096,
                  video_id: str | None, stage: str, cache_system: bool = False,
                  effort: str | None = None) -> Any:
    text = complete(messages=messages, system=system, max_tokens=max_tokens, video_id=video_id,
                    stage=stage, cache_system=cache_system, effort=effort, json_mode=True)
    return parse_json(text)


# ---------- 结构化输出（提案 0001 §8） ----------

M = TypeVar("M", bound=BaseModel)

_MAX_FEEDBACK_PROBLEMS = 20


def structured_mode(stage: str = "") -> str:
    """本次结构化调用的方式：anthropic（服务商为 Anthropic）或 openai_compat.structured_output_mode() 的结果。"""
    from . import settings as llm_settings

    if llm_settings.uses_deepseek(stage):
        return "json_object"
    if not _use_openai(stage):
        return "anthropic"
    from . import openai_compat

    return openai_compat.structured_output_mode()


def complete_structured(*, messages: list[dict], response_model: type[M], system: str | list | None = None,
                        max_tokens: int = 4096, video_id: str | None, stage: str, effort: str | None = None,
                        cache_system: bool = False, retries: int = 1,
                        check: Callable[[M], list[str] | None] | None = None,
                        schema_hint: bool = True) -> M:
    """一次结构化调用：返回通过 `response_model` 校验的 Pydantic 对象。

    参数与 `complete_json` 相同（messages / system / max_tokens / video_id / stage / effort / cache_system），
    用量记录、费用计算、推理强度、提示缓存的处理也相同；另外：
    - response_model：返回模型，例如 `schemas.OutlineResponse` 或 `schemas.slides_response_model(...)` 生成的本批模型。
    - retries：输出不合格时带错误说明重试的次数（默认 1，即最多请求 2 次）。
    - check：可选的额外检查，收到合格对象后调用，返回问题列表（英文，会原样发给模型）；非空时同样带说明重试。
      例如读幻灯片检查「每个出现 / T 恰好一次」。
    - schema_hint：退回 JSON 模式时，是否把 JSON Schema 附在系统提示末尾（默认附上）。

    发送方式（`structured_mode()`）：
    - OpenAI 兼容接口，且接口地址主机名为 api.openai.com：`response_format` = 严格 JSON Schema
      （`strict: true`，schema 由 `openai_compat.strict_json_schema` 从 Pydantic 模型生成）。
      schema 超出严格模式限制（枚举值合计 > 1000 等）时记警告并退回 JSON 模式。
    - 其他兼容接口（DeepSeek 等）：JSON 模式（`{"type": "json_object"}`）。可用设置项
      `app_settings["openai_structured_outputs"]` / 环境变量 `OPENAI_STRUCTURED_OUTPUTS`
      （auto / json_schema / json_object / none）覆盖，见 `openai_compat.structured_output_mode`。
    - `llm_provider == "anthropic"`：走原有 `complete(json_mode=True)`，本地校验。
    退回 JSON 模式时都用同一个 Pydantic 模型严格校验（多余字段、类型不符、ID 不在枚举内都算错误）。

    异常（都是 LLMError 的子类，调用方据此把该条计为 failed，§5.3）：
    - LLMRefusalError：拒答或被内容过滤；LLMTruncatedError：`finish_reason == "length"`。这两种不在这里重试
      （同样的请求大概率再次失败），由调用方决定是否调整后重试。
    - LLMInvalidOutputError：重试 `retries` 次后仍不合格。
    - 其他 LLMError：网络、Key、限流等，与 `complete` 相同。
    每次请求（包括被拒、被截断、不合格的）都会记入 api_usage。重试会重新发送整个请求（含图片）。

    用法：
        from app.llm import client as llm, schemas
        Model = schemas.slides_response_model(["S012"], ["S012.1"], ["T0031", "T0032"])
        resp = llm.complete_structured(messages=msgs, system=SYSTEM, response_model=Model, max_tokens=8000,
                                       video_id=vid, stage="read_slides", effort="low",
                                       check=lambda r: schemas.check_speech_categories(
                                           r.slides[0].speech_categories, ["T0031", "T0032"]))
        resp.slides[0].title
    """
    from . import openai_compat

    mode = structured_mode(stage)
    schema = openai_compat.strict_json_schema(response_model)
    if mode == "json_schema":
        problems = openai_compat.schema_limit_problems(schema)
        if problems:
            log.warning("%s 超出严格模式限制（%s），退回 JSON 模式", response_model.__name__, "；".join(problems))
            mode = "json_object"
    call_system = system
    if mode != "json_schema" and schema_hint:
        call_system = _append_system(system, openai_compat.schema_hint(schema))

    convo = list(messages)
    for attempt in range(retries + 1):
        if mode == "anthropic":
            text = complete(messages=convo, system=call_system, max_tokens=max_tokens, video_id=video_id,
                            stage=stage, cache_system=cache_system, effort=effort, json_mode=True)
        else:
            text = _openai_structured_once(convo, call_system, max_tokens, video_id, stage, effort, cache_system,
                                           mode, response_model, schema)
        problems = _validate_structured(text, response_model, check)
        if isinstance(problems, BaseModel):
            return problems  # type: ignore[return-value]
        if attempt < retries:
            log.warning("结构化输出不合格（%s），带说明重试 %d：%s", response_model.__name__, attempt + 1,
                        "; ".join(problems[:3]))
            convo = convo + [{"role": "assistant", "content": text},
                             {"role": "user", "content": _feedback(problems)}]
            continue
        raise LLMInvalidOutputError(
            f"模型输出不符合格式要求（已重试 {retries} 次）：" + "；".join(problems[:5]),
            raw_text=text, problems=problems)
    raise AssertionError("unreachable")


def _append_system(system: str | list | None, extra: str) -> str | list:
    if system is None:
        return extra
    if isinstance(system, str):
        return f"{system}\n\n{extra}"
    return [*system, {"type": "text", "text": extra}]


def _openai_structured_once(messages, system, max_tokens, video_id, stage, effort, cache_system, mode,
                            response_model, schema) -> str:
    from . import openai_compat

    req = _openai_request(messages, system, max_tokens, stage, effort, mode == "json_object",
                          _cache_key(cache_system, video_id, stage))
    if mode == "json_schema":
        req["response_format"] = openai_compat.response_format_json_schema(response_model, schema)
    try:
        raw = openai_compat.complete_raw(req)
    except Exception as e:  # noqa: BLE001
        raise openai_compat.to_llm_error(e) from e
    _record_openai(raw.usage, video_id=video_id, stage=stage, model=req["model"], images=_count_images(messages))
    if raw.refusal:
        raise LLMRefusalError(f"模型拒绝了该请求：{raw.refusal}")
    if raw.finish_reason == "content_filter":
        raise LLMRefusalError("模型输出被内容过滤拦截")
    if raw.finish_reason == "length":
        raise LLMTruncatedError(f"模型输出被截断（达到输出上限 {max_tokens} token，推理 token 也计入上限），"
                                "可调低推理强度、减少每批数量或提高上限后重试")
    return raw.text


def _validate_structured(text: str, response_model: type[M], check) -> M | list[str]:
    """合格时返回模型对象，否则返回问题列表（英文，用作重试反馈）。"""
    try:
        data = parse_json(text)
    except ValueError:
        return ["The reply is not a valid JSON object."]
    try:
        obj = response_model.model_validate(data)
    except ValidationError as e:
        return [_format_validation_error(err) for err in e.errors()[:_MAX_FEEDBACK_PROBLEMS]]
    if check is not None:
        problems = [p for p in (check(obj) or []) if p]
        if problems:
            return problems
    return obj


def _format_validation_error(err: dict) -> str:
    loc = "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in err.get("loc", ())).lstrip(".") or "(root)"
    msg = err.get("msg", "invalid")
    inp = err.get("input")
    if err.get("type") in ("literal_error", "enum") and isinstance(inp, str):
        msg += f" (got {inp!r})"
    return f"{loc}: {msg}"


def _feedback(problems: list[str]) -> str:
    return ("Your previous reply failed validation:\n- " + "\n- ".join(problems[:_MAX_FEEDBACK_PROBLEMS])
            + "\nReply again with the complete, corrected JSON object only. "
              "Keep the same language and content where they were correct.")
