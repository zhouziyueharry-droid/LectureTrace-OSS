"""OpenAI 兼容接口（OpenAI、DeepSeek 等）：Chat Completions。

输入沿用本项目统一的 Anthropic 风格消息（文字块 / base64 图片块），这里转换为 OpenAI 格式。
返回 (文本, 用量)；用量里的 output 已包含推理 token（OpenAI 按输出计费）。
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator
from urllib.parse import urlparse

from .. import db, keys

log = logging.getLogger(__name__)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0          # 命中缓存的输入（按缓存价计费）
    reasoning_tokens: int = 0
    cache_write_tokens: int = 0     # 写入缓存的输入（GPT-5.6 按 1.25× 输入价计费）
    service_tier: str | None = None  # 响应里实际的处理档位（flex / default 等）；接口未返回时为 None，按标准价计


_client = None
_client_sig: tuple | None = None
_lock = threading.Lock()


def get_client(service: str = "openai"):
    """按当前 Key 与接口地址缓存客户端。"""
    from .client import LLMError  # 避免循环导入

    global _client, _client_sig
    key = keys.get_key(service)
    if not key:
        raise LLMError(f"未填写 {'DeepSeek' if service == 'deepseek' else 'OpenAI'} API Key，请到设置页填写")
    sig = (key, keys.DEEPSEEK_BASE_URL if service == 'deepseek' else keys.openai_base_url())
    with _lock:
        if _client is None or _client_sig != sig:
            import openai

            _client = openai.OpenAI(api_key=key, base_url=sig[1], max_retries=4, timeout=600.0)
            _client_sig = sig
        return _client


_REASONING_RE = re.compile(r"^(?:gpt-(\d+)|o\d)")


def is_reasoning_model(model: str) -> bool:
    """GPT-5 及以后的 gpt-N 和 o 系列是推理模型：用 max_completion_tokens、可设 reasoning_effort。

    按版本号判断而不是列前缀：只列 gpt-5 时 gpt-6-sol 被当成普通模型，请求带 max_tokens 被拒（HTTP 400，2026-09-28）。"""
    m = _REASONING_RE.match(model)
    return bool(m) and (m.group(1) is None or int(m.group(1)) >= 5)


def supports_explicit_cache() -> bool:
    """显式提示缓存参数只对 OpenAI 官方接口发送；DeepSeek 等兼容接口不认识这些参数。"""
    return "api.openai.com" in keys.openai_base_url()


def convert_messages(messages: list[dict], system: str | list | None, cache_breakpoint: bool = False) -> list[dict]:
    out: list[dict] = []
    if system:
        text = system if isinstance(system, str) else "\n\n".join(b.get("text", "") for b in system)
        if cache_breakpoint:
            # 在系统消息（整节课资料）末尾设缓存点：追问时这段前缀命中缓存，按 0.1× 输入价计费
            out.append({"role": "system", "content": [
                {"type": "text", "text": text, "prompt_cache_breakpoint": {"mode": "explicit"}}]})
        else:
            out.append({"role": "system", "content": text})
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            out.append({"role": m["role"], "content": content})
            continue
        parts = []
        for b in content:
            if b.get("type") == "image":
                src = b["source"]
                parts.append({"type": "image_url", "image_url": {
                    "url": f"data:{src.get('media_type', 'image/jpeg')};base64,{src['data']}", "detail": "high"}})
            elif b.get("type") == "text":
                parts.append({"type": "text", "text": b["text"]})
        # 助手消息只能是纯文本
        if m["role"] == "assistant":
            out.append({"role": "assistant", "content": "".join(p.get("text", "") for p in parts)})
        else:
            out.append({"role": m["role"], "content": parts})
    return out


def build_request(*, model: str, messages: list[dict], system, max_tokens: int, effort: str | None,
                  json_mode: bool, cache_key: str | None = None, service_tier: str | None = None) -> dict[str, Any]:
    """cache_key 非空表示这类请求会反复发送同一前缀（问答）：在系统消息末尾设缓存点，并按 key 分组。
    其余请求一律不写缓存：GPT-5.6 默认会自动写入缓存并按 1.25× 输入价收费，一次性请求用不上。

    service_tier：设置的处理档位（flex | standard | None）。flex 且接口为 OpenAI 官方时加 `service_tier: "flex"`；
    DeepSeek 等兼容接口不认识该参数，一律不加。standard 不发送该参数（即项目默认，标准价）。"""
    deepseek = model == "deepseek-flash"
    explicit = not deepseek and supports_explicit_cache()
    breakpoint = bool(cache_key) and explicit
    req: dict[str, Any] = {"model": model, "messages": convert_messages(messages, system, breakpoint)}
    if not deepseek and service_tier == "flex" and is_official_openai():
        req["service_tier"] = "flex"
    if explicit:
        extra: dict[str, Any] = {"prompt_cache_options": {"mode": "explicit"}}
        if cache_key:
            extra["prompt_cache_key"] = cache_key
        req["extra_body"] = extra
    if deepseek:
        req["max_tokens"] = max_tokens
        req["extra_body"] = {"thinking": {"type": "disabled" if effort == "none" else "enabled"}}
        if effort != "none":
            req["reasoning_effort"] = {"medium": "high", "xhigh": "high"}.get(effort, effort or "high")
    elif is_reasoning_model(model):
        # 推理模型用 max_completion_tokens（含推理 token）；推理强度可调
        req["max_completion_tokens"] = max_tokens
        if effort:
            req["reasoning_effort"] = effort
    else:
        req["max_tokens"] = max_tokens
    if json_mode:
        req["response_format"] = {"type": "json_object"}
    return req


def _usage(u, service_tier: str | None = None) -> Usage:
    """service_tier：响应（或流式最后一个 chunk）里的 service_tier，原样记下，计价时只认 "flex"。"""
    tier = service_tier if isinstance(service_tier, str) and service_tier else None
    if u is None:
        return Usage(service_tier=tier)
    cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
    reasoning = getattr(getattr(u, "completion_tokens_details", None), "reasoning_tokens", 0) or 0
    written = getattr(getattr(u, "prompt_tokens_details", None), "cache_write_tokens", 0) or 0
    # DeepSeek 用 prompt_cache_hit_tokens 表示缓存命中
    cached = cached or getattr(u, "prompt_cache_hit_tokens", 0) or 0
    return Usage(int(u.prompt_tokens or 0), int(u.completion_tokens or 0), int(cached), int(reasoning),
                 int(written), tier)


# ---------- 处理档位：Flex ----------
# 官方 Flex 指南（https://developers.openai.com/api/docs/guides/flex-processing ，2026-09-28 查询）：
# - 请求加 service_tier="flex"；响应更慢，建议把客户端超时从默认 10 分钟调到 15 分钟（示例 timeout=900.0）。
# - 容量不足时返回 429 Resource Unavailable，且不收费；建议指数退避重试，或改用标准档重试。
#
# 重试的组合（SDK 客户端 max_retries=4 不变，对所有请求一视同仁地处理 408/409/429/5xx/断线）：
# 1. SDK 自带重试：429 会被 SDK 自动重试 4 次，间隔 0.5→1→2→4 秒（openai/_constants.py 的 INITIAL_RETRY_DELAY /
#    MAX_RETRY_DELAY，带 0–25% 抖动；有 Retry-After 时按它，最长 60 秒）。这相当于第一段短退避，约 7 秒。
# 2. SDK 用完后仍是「资源不可用」：这里再按 FLEX_RETRY_DELAYS_S 退避重试 flex（每次仍含 SDK 的短重试）。
#    资源不可用不收费，多几次请求不花钱，只花时间。
# 3. 仍不行：改用标准档（service_tier="default"）重试一次并记警告，按标准价计费，保证流程不中断。
#    用 "default" 而不是 "auto"：auto 取项目设置，若项目默认档被设成 flex 会再次落到 flex。
# 典型情况下改用标准档前约等待 3×7 + 10 + 30 ≈ 60 秒、共发 15 次 flex 请求。
# 读幻灯片是顺序的多批请求，容量紧张时每批都等 60 秒太慢：回退后 FLEX_FALLBACK_COOLDOWN_S 内的 flex 请求
# 只试一轮（SDK 短重试），不再做第 2 段的长退避，仍失败就直接改用标准档。
# 普通限流（rate_limit_exceeded、额度 / 花费上限等）不算资源不可用：照旧抛出，不改档位。
FLEX_TIMEOUT_S = 900.0                      # 官方示例：15 分钟
FLEX_RETRY_DELAYS_S = (10.0, 30.0)          # SDK 重试用完后，再退避重试 flex 的间隔（秒）
FLEX_FALLBACK_COOLDOWN_S = 300.0            # 回退标准档后，这段时间内的 flex 请求跳过第 2 段退避
FALLBACK_SERVICE_TIER = "default"

# 官方文档只写「429 Resource Unavailable」，没有列出错误体里的 code；第三方文档（LBL CBorg）写作 resource_unavailable。
# 识别规则：HTTP 429，且 code / type 为 resource_unavailable，或 message 含 "resource unavailable"（忽略大小写、
# 空格 / 下划线 / 连字符）；code 属于下列普通限流 / 额度错误的一律不算（官方 Error codes 页，2026-09-28）。
RESOURCE_UNAVAILABLE_CODES = frozenset({"resource_unavailable"})
RATE_LIMIT_CODES = frozenset({
    "rate_limit_exceeded", "slow_down", "insufficient_quota", "organization_spend_limit_exceeded",
    "project_spend_limit_exceeded", "organization_usage_limit_exceeded", "credit_balance_exhausted"})
_RESOURCE_UNAVAILABLE_RE = re.compile(r"resource[\s_-]*unavailable", re.I)

_sleep = time.sleep                          # 测试里替换
_flex_fallback_at: float | None = None       # 最近一次改用标准档的时刻（time.monotonic）
_flex_lock = threading.Lock()


def effective_service_tier(purpose: str) -> str:
    """该用途实际会用的档位：设置为 flex 且接口为 OpenAI 官方时是 flex，否则 standard。"""
    from . import settings as llm_settings

    if llm_settings.uses_deepseek(purpose):
        return "standard"
    return "flex" if llm_settings.service_tier_for(purpose) == "flex" and is_official_openai() else "standard"


def is_resource_unavailable(e: BaseException) -> bool:
    """是否为 flex 容量不足的 429 Resource Unavailable（而不是普通限流）。"""
    import openai

    if not isinstance(e, openai.APIStatusError) or e.status_code != 429:
        return False
    code = str(getattr(e, "code", None) or "").strip().lower()
    if code in RATE_LIMIT_CODES:
        return False
    if code in RESOURCE_UNAVAILABLE_CODES or str(getattr(e, "type", None) or "").strip().lower() in RESOURCE_UNAVAILABLE_CODES:
        return True
    # SDK 的 e.message 形如 "Error code: 429 - {...}"；错误体里的 message 另外看
    body = getattr(e, "body", None)
    texts = [str(getattr(e, "message", None) or e), str(body.get("message") or "") if isinstance(body, dict) else ""]
    return any(_RESOURCE_UNAVAILABLE_RE.search(t) for t in texts)


def _in_fallback_cooldown() -> bool:
    with _flex_lock:
        return _flex_fallback_at is not None and time.monotonic() - _flex_fallback_at < FLEX_FALLBACK_COOLDOWN_S


def _mark_fallback() -> None:
    global _flex_fallback_at
    with _flex_lock:
        _flex_fallback_at = time.monotonic()


def create(req: dict, **extra):
    """chat.completions.create 的封装：flex 请求加长超时，遇到资源不可用时退避重试，最后改用标准档。
    extra 为 stream=True 等不属于请求体的参数。"""
    client = get_client("deepseek") if req.get("model") == "deepseek-flash" else get_client()
    if req.get("service_tier") != "flex":
        return client.chat.completions.create(**req, **extra)
    delays = () if _in_fallback_cooldown() else FLEX_RETRY_DELAYS_S
    for attempt in range(len(delays) + 1):
        try:
            return client.chat.completions.create(**req, **extra, timeout=FLEX_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001
            if not is_resource_unavailable(e):
                raise
            if attempt < len(delays):
                log.info("Flex 容量不足（%s），%.0f 秒后重试 flex（第 %d 次）", req.get("model"), delays[attempt],
                         attempt + 1)
                _sleep(delays[attempt])
    log.warning("Flex 容量不足，已重试 %d 轮，本次改用标准档（%s，按标准价计费）", len(delays) + 1, req.get("model"))
    _mark_fallback()
    return client.chat.completions.create(**{**req, "service_tier": FALLBACK_SERVICE_TIER}, **extra)


def complete(req: dict) -> tuple[str, Usage]:
    resp = create(req)
    choice = resp.choices[0]
    if choice.finish_reason == "length" and not (choice.message.content or "").strip():
        from .client import LLMError

        raise LLMError("模型输出被截断（推理占满了输出上限），可调低推理强度或提高上限后重试")
    return choice.message.content or "", _usage(resp.usage, getattr(resp, "service_tier", None))


@dataclass
class RawCompletion:
    """一次非流式调用的原始结果，供结构化调用自行判断拒答、截断并记账。"""
    text: str
    usage: Usage
    finish_reason: str | None
    refusal: str | None


def complete_raw(req: dict) -> RawCompletion:
    """与 complete 相同的请求，但不在这里判断截断：交给调用方在记账之后处理。"""
    resp = create(req)
    choice = resp.choices[0]
    msg = choice.message
    return RawCompletion(text=getattr(msg, "content", None) or "",
                         usage=_usage(resp.usage, getattr(resp, "service_tier", None)),
                         finish_reason=getattr(choice, "finish_reason", None),
                         refusal=getattr(msg, "refusal", None) or None)


# ---------- 结构化输出 ----------

STRUCTURED_MODES = ("auto", "json_schema", "json_object", "none")
STRUCTURED_SETTING_KEY = "openai_structured_outputs"   # app_settings 中的键；.env 后备为 OPENAI_STRUCTURED_OUTPUTS
OFFICIAL_OPENAI_HOST = "api.openai.com"


def is_official_openai() -> bool:
    """接口地址的主机名是否为 OpenAI 官方（api.openai.com）。"""
    return (urlparse(keys.openai_base_url()).hostname or "").lower() == OFFICIAL_OPENAI_HOST


def structured_output_mode() -> str:
    """OpenAI 兼容接口上，结构化调用用哪种 response_format：

    - 设置项 `app_settings["openai_structured_outputs"]`（未设置时读环境变量 `OPENAI_STRUCTURED_OUTPUTS`），
      取值 auto / json_schema / json_object / none，默认 auto；无法识别的值按 auto 处理。
    - auto：接口地址主机名是 api.openai.com → `json_schema`（严格模式）；其余（DeepSeek 等）→ `json_object`。
    - json_schema：强制严格 JSON Schema（例如 Azure、OpenRouter 等也支持的兼容接口）。
    - json_object：JSON 模式；none：不发送 response_format（接口连 JSON 模式都不支持时）。
      这两种都在本地用同一个 Pydantic 模型严格校验。
    """
    raw = db.get_setting(STRUCTURED_SETTING_KEY) or os.getenv("OPENAI_STRUCTURED_OUTPUTS") or "auto"
    mode = str(raw).strip().lower()
    if mode not in STRUCTURED_MODES:
        log.warning("无法识别的结构化输出设置 %r，按 auto 处理", raw)
        mode = "auto"
    if mode == "auto":
        return "json_schema" if is_official_openai() else "json_object"
    return mode


# OpenAI 严格模式的限制（官方文档 Structured Outputs → Supported schemas）
MAX_ENUM_VALUES = 1000             # 整个 schema 的枚举值合计
MAX_LARGE_ENUM_CHARS = 7500        # 单个枚举超过 250 个值时，值的总长度上限
MAX_NESTING = 10
MAX_PROPERTIES = 5000


def strict_json_schema(model: type) -> dict[str, Any]:
    """Pydantic 模型 → OpenAI 严格模式可接受的 JSON Schema（不修改模型本身）。

    处理方式：
    - 每个 object：`required` = 全部属性，`additionalProperties: false`。可选字段（`X | None`）
      生成 `anyOf [X, null]`，模型必须显式输出 null；有默认值的字段同样变成必填。
    - 删除所有 `default`（严格模式不接受）和节点上的 `title`（只是噪音；属性名为 title 的字段不受影响）。
    - `const` 改写为单值 `enum`。
    - `$ref` 与其他关键字并列时（例如带 description 的嵌套模型），把引用内联展开；单独的 `$ref` 保留，`$defs` 照常输出。
    - 任意键的对象（`dict[str, X]`，即 `additionalProperties` 为 schema 或 `patternProperties`）严格模式不支持，
      直接抛 TypeError：请改成 `[{id, ...}]` 数组（例如 speech_categories）。
    """
    schema = copy.deepcopy(model.model_json_schema())
    return _strictify(schema, root=schema, path="#")


def _strictify(node: Any, *, root: dict, path: str) -> Any:
    if isinstance(node, list):
        return [_strictify(n, root=root, path=f"{path}/{i}") for i, n in enumerate(node)]
    if not isinstance(node, dict):
        return node

    ref = node.get("$ref")
    if ref and len(node) > 1:
        resolved = _resolve_ref(root, ref)
        merged = {**resolved, **{k: v for k, v in node.items() if k != "$ref"}}
        return _strictify(merged, root=root, path=path)

    ap = node.get("additionalProperties")
    if isinstance(ap, dict) or "patternProperties" in node:
        raise TypeError(f"严格模式不支持任意键的对象（{path}），请改成数组")

    out: dict[str, Any] = {}
    for key, val in node.items():
        if key in ("default", "title"):
            continue
        if key == "const":
            out["enum"] = [val]
        elif key in ("properties", "$defs", "definitions"):
            out[key] = {k: _strictify(v, root=root, path=f"{path}/{key}/{k}") for k, v in val.items()}
        elif key in ("items", "anyOf", "allOf", "oneOf", "prefixItems", "not"):
            out[key] = _strictify(val, root=root, path=f"{path}/{key}")
        else:
            out[key] = val
    if out.get("type") == "object" or "properties" in out:
        out.setdefault("properties", {})
        out["required"] = list(out["properties"])
        out["additionalProperties"] = False
    return out


def _resolve_ref(root: dict, ref: str) -> dict:
    if not ref.startswith("#/"):
        raise TypeError(f"无法解析的 $ref：{ref}")
    node: Any = root
    for part in ref[2:].split("/"):
        node = node[part]
    return copy.deepcopy(node)


def schema_limit_problems(schema: dict) -> list[str]:
    """检查 schema 是否超出严格模式的限制（枚举数量、嵌套层数、属性数）。返回问题列表，空表示可用。"""
    enum_total = props = 0
    big_enum_chars: list[int] = []
    max_depth = 0

    def walk(n: Any, depth: int) -> None:
        nonlocal enum_total, props, max_depth
        if isinstance(n, list):
            for x in n:
                walk(x, depth)
            return
        if not isinstance(n, dict):
            return
        if isinstance(n.get("enum"), list):
            values = n["enum"]
            enum_total += len(values)
            if len(values) > 250:
                big_enum_chars.append(sum(len(str(v)) for v in values))
        if isinstance(n.get("properties"), dict):
            props += len(n["properties"])
            max_depth = max(max_depth, depth + 1)
            for v in n["properties"].values():
                walk(v, depth + 1)
        for key in ("items", "anyOf", "allOf", "$defs", "definitions"):
            v = n.get(key)
            if isinstance(v, dict) and key in ("$defs", "definitions"):
                for d in v.values():
                    walk(d, 0)
            elif v is not None:
                walk(v, depth)

    walk(schema, 0)
    problems = []
    if enum_total > MAX_ENUM_VALUES:
        problems.append(f"枚举值合计 {enum_total} 个，超过 {MAX_ENUM_VALUES}")
    if any(c > MAX_LARGE_ENUM_CHARS for c in big_enum_chars):
        problems.append(f"单个枚举的值总长度超过 {MAX_LARGE_ENUM_CHARS} 字符")
    if max_depth > MAX_NESTING:
        problems.append(f"嵌套 {max_depth} 层，超过 {MAX_NESTING}")
    if props > MAX_PROPERTIES:
        problems.append(f"属性合计 {props} 个，超过 {MAX_PROPERTIES}")
    return problems


def response_format_json_schema(model: type, schema: dict | None = None) -> dict[str, Any]:
    """严格模式的 response_format。name 取模型类名（只保留字母数字、下划线、连字符，最长 64）。"""
    name = re.sub(r"[^A-Za-z0-9_-]", "_", getattr(model, "__name__", "response"))[:64] or "response"
    return {"type": "json_schema",
            "json_schema": {"name": name, "strict": True, "schema": schema or strict_json_schema(model)}}


def schema_hint(schema: dict) -> str:
    """退回 JSON 模式时附在系统提示末尾的格式说明（JSON 模式要求提示里出现 "JSON"）。"""
    return ("Reply with a single JSON object (no code fences, no extra text) that conforms to this JSON Schema. "
            "Every property is required; use null only where the schema allows it.\n"
            + json.dumps(schema, ensure_ascii=False, separators=(",", ":")))


def stream(req: dict, on_usage: Callable[[Usage], None]) -> Iterator[str]:
    # 资源不可用的 429 在建立流时就返回（尚未产出任何文本），所以同样可以退避后改用标准档
    s = create(req, stream=True, stream_options={"include_usage": True})
    usage = tier = None
    for chunk in s:
        if getattr(chunk, "usage", None) is not None:
            usage = chunk.usage
        tier = getattr(chunk, "service_tier", None) or tier
        for c in chunk.choices or []:
            if c.delta and c.delta.content:
                yield c.delta.content
    on_usage(_usage(usage, tier))


def to_llm_error(e: Exception):
    from .client import LLMError
    import openai

    if isinstance(e, LLMError):
        return e
    if isinstance(e, openai.AuthenticationError):
        return LLMError("OpenAI API Key 无效或已失效")
    if isinstance(e, openai.NotFoundError):
        return LLMError(f"模型或接口不存在：{getattr(e, 'message', e)}")
    if isinstance(e, openai.RateLimitError):
        return LLMError("接口限流或额度不足，请稍后重试或检查账户余额")
    if isinstance(e, openai.BadRequestError):
        return LLMError(f"请求被拒绝（HTTP 400）：{getattr(e, 'message', e)}")
    if isinstance(e, openai.APIStatusError):
        return LLMError(f"模型接口错误（HTTP {e.status_code}）：{getattr(e, 'message', e)}")
    if isinstance(e, (openai.APIConnectionError, openai.APITimeoutError)):
        return LLMError("无法连接模型接口，请检查网络或接口地址")
    return LLMError(f"调用模型失败：{e}")
