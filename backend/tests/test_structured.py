"""结构化输出（提案 0001 §8）：严格 JSON Schema、兼容接口退回 JSON 模式、校验失败重试、拒答、截断、ID 枚举约束。
全部用假客户端，不发真实请求。"""
from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace
from typing import Literal, Optional

import pytest
from pydantic import BaseModel, Field, ValidationError

from app import config, db, keys
from app.llm import client as llm, openai_compat, schemas


def _resp(content, finish_reason="stop", refusal=None):
    usage = SimpleNamespace(prompt_tokens=1000, completion_tokens=200,
                            prompt_tokens_details=SimpleNamespace(cached_tokens=0),
                            completion_tokens_details=SimpleNamespace(reasoning_tokens=100))
    msg = SimpleNamespace(content=content, refusal=refusal)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason=finish_reason)], usage=usage)


@pytest.fixture
def fake_openai(tmp_settings, set_settings, monkeypatch):
    """OpenAI 官方接口 + 假客户端。fake.replies 依次返回；fake.calls 记录请求。"""
    set_settings(dataclasses.replace(config.settings, llm_provider="openai"))
    keys.update({"openai": "sk-test", "openai_base_url": "https://api.openai.com/v1"})
    monkeypatch.delenv("OPENAI_STRUCTURED_OUTPUTS", raising=False)
    fake = SimpleNamespace(calls=[], replies=[])

    def create(**req):
        fake.calls.append(req)
        return fake.replies.pop(0)

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(openai_compat, "get_client", lambda: client)
    return fake


BATCH = dict(slide_ids=["S012"], occurrence_ids=["S012.1", "S012.2"], t_ids=["T0031", "T0032"])


def _slides_json(slide_id="S012", t2="T0032"):
    return json.dumps({"slides": [{
        "id": slide_id, "title": "Batch Normalization", "visual_content": "- x\n[handwritten] y",
        "occurrences": [{"id": "S012.1", "spoken_summary": "a"}, {"id": "S012.2", "spoken_summary": "b"}],
        "speech_categories": [{"id": "T0031", "category": "lecture"}, {"id": t2, "category": "admin"}]}]})


def _call(model, **kw):
    return llm.complete_structured(messages=[{"role": "user", "content": "read"}], system="sys",
                                   response_model=model, max_tokens=800, video_id="v1", stage="read_slides",
                                   effort="low", **kw)


# ---------- 发送方式 ----------

def test_official_api_sends_strict_schema(fake_openai):
    Model = schemas.slides_response_model(**BATCH)
    fake_openai.replies = [_resp(_slides_json())]
    out = _call(Model)
    assert isinstance(out, schemas.SlidesResponse) and out.slides[0].title == "Batch Normalization"
    assert schemas.speech_categories_dict(out.slides[0].speech_categories) == {"T0031": "lecture", "T0032": "admin"}

    req = fake_openai.calls[0]
    rf = req["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["name"] == "SlidesResponseBatch"
    schema = rf["json_schema"]["schema"]
    assert schema["additionalProperties"] is False and schema["required"] == ["slides"]
    slide = schema["$defs"]["SlideRecordBatch"]
    assert slide["properties"]["id"]["enum"] == ["S012"]
    assert slide["required"] == ["id", "title", "visual_content", "occurrences", "speech_categories"]
    assert schema["$defs"]["SpeechCategoryBatch"]["properties"]["id"]["enum"] == ["T0031", "T0032"]
    # 与 complete_json 相同：推理强度、一次性请求不写缓存、系统提示不附 schema
    assert req["reasoning_effort"] == "low" and req["max_completion_tokens"] == 800
    assert req["extra_body"] == {"prompt_cache_options": {"mode": "explicit"}}
    assert req["messages"][0] == {"role": "system", "content": "sys"}
    row = db.fetch_one("SELECT * FROM api_usage")
    assert row["stage"] == "read_slides" and row["video_id"] == "v1" and row["input_tokens"] == 1000
    assert row["cost_usd"] == pytest.approx((1000 * 4 + 200 * 20) / 1e6)


def test_compat_api_falls_back_to_json_mode(fake_openai):
    keys.update({"openai_base_url": "https://api.deepseek.com"})
    assert llm.structured_mode() == "json_object"
    fake_openai.replies = [_resp("```json\n" + _slides_json() + "\n```")]
    out = _call(schemas.slides_response_model(**BATCH))
    assert out.slides[0].id == "S012"
    req = fake_openai.calls[0]
    assert req["response_format"] == {"type": "json_object"} and "extra_body" not in req
    system = req["messages"][0]["content"]
    assert system.startswith("sys\n\n") and "JSON Schema" in system and '"enum":["S012"]' in system


def test_mode_override_setting(fake_openai, monkeypatch):
    keys.update({"openai_base_url": "https://openrouter.ai/api/v1"})
    db.set_setting(openai_compat.STRUCTURED_SETTING_KEY, "json_schema")
    assert llm.structured_mode() == "json_schema"
    db.set_setting(openai_compat.STRUCTURED_SETTING_KEY, "none")
    fake_openai.replies = [_resp(_slides_json())]
    _call(schemas.SlidesResponse)
    assert "response_format" not in fake_openai.calls[0]
    db.set_setting(openai_compat.STRUCTURED_SETTING_KEY, None)
    monkeypatch.setenv("OPENAI_STRUCTURED_OUTPUTS", "json_object")
    keys.update({"openai_base_url": "https://api.openai.com/v1"})
    assert llm.structured_mode() == "json_object"
    monkeypatch.setenv("OPENAI_STRUCTURED_OUTPUTS", "bogus")
    assert llm.structured_mode() == "json_schema"      # 无法识别 → auto


def test_schema_over_limits_falls_back_to_json_mode(fake_openai):
    many = [f"T{i:04d}" for i in range(1200)]
    Model = schemas.slides_response_model(["S001"], ["S001.1"], many)
    fake_openai.replies = [_resp(json.dumps({"slides": []}))]
    _call(Model)
    assert fake_openai.calls[0]["response_format"] == {"type": "json_object"}


# ---------- 校验与重试 ----------

def test_invalid_id_is_retried_with_feedback(fake_openai):
    keys.update({"openai_base_url": "https://api.deepseek.com"})
    fake_openai.replies = [_resp(_slides_json(t2="T9999")), _resp(_slides_json())]
    out = _call(schemas.slides_response_model(**BATCH))
    assert out.slides[0].speech_categories[1].id == "T0032"
    assert len(fake_openai.calls) == 2
    retry_msgs = fake_openai.calls[1]["messages"]
    assert retry_msgs[-2]["role"] == "assistant" and "T9999" in retry_msgs[-2]["content"]
    assert retry_msgs[-1]["role"] == "user" and "'T9999'" in retry_msgs[-1]["content"]
    assert "slides[0].speech_categories[1].id" in retry_msgs[-1]["content"]
    assert db.fetch_one("SELECT COUNT(*) AS n FROM api_usage")["n"] == 2     # 每次请求都记账


def test_invalid_after_retries_raises(fake_openai):
    fake_openai.replies = [_resp("not json"), _resp('{"slides": [{"id": "S012"}]}')]
    with pytest.raises(llm.LLMInvalidOutputError, match="已重试 1 次") as ei:
        _call(schemas.slides_response_model(**BATCH))
    assert isinstance(ei.value, llm.LLMError) and ei.value.raw_text == '{"slides": [{"id": "S012"}]}'
    assert any("title" in p for p in ei.value.problems)

    fake_openai.replies = [_resp("not json")]
    with pytest.raises(llm.LLMInvalidOutputError, match="已重试 0 次"):
        _call(schemas.SlidesResponse, retries=0)
    assert len(fake_openai.calls) == 3


def test_extra_fields_are_rejected(fake_openai):
    bad = json.loads(_slides_json())
    bad["slides"][0]["note"] = "x"
    fake_openai.replies = [_resp(json.dumps(bad))]
    with pytest.raises(llm.LLMInvalidOutputError, match="note"):
        _call(schemas.SlidesResponse, retries=0)


def test_check_callback_triggers_retry(fake_openai):
    missing = json.loads(_slides_json())
    missing["slides"][0]["speech_categories"] = [{"id": "T0031", "category": "lecture"}]
    fake_openai.replies = [_resp(json.dumps(missing)), _resp(_slides_json())]
    check = lambda r: schemas.check_speech_categories(r.slides[0].speech_categories, BATCH["t_ids"])  # noqa: E731
    out = _call(schemas.slides_response_model(**BATCH), check=check)
    assert len(out.slides[0].speech_categories) == 2
    assert "missing these IDs: T0032" in fake_openai.calls[1]["messages"][-1]["content"]


# ---------- 拒答与截断 ----------

def test_refusal_becomes_chinese_error(fake_openai):
    fake_openai.replies = [_resp(None, refusal="I can't help with that.")]
    with pytest.raises(llm.LLMRefusalError, match="模型拒绝了该请求"):
        _call(schemas.SlidesResponse)
    assert len(fake_openai.calls) == 1                                     # 不重试
    assert db.fetch_one("SELECT COUNT(*) AS n FROM api_usage")["n"] == 1   # 仍然记账

    fake_openai.replies = [_resp(None, finish_reason="content_filter")]
    with pytest.raises(llm.LLMRefusalError, match="内容过滤"):
        _call(schemas.SlidesResponse)


def test_truncation_becomes_chinese_error(fake_openai):
    fake_openai.replies = [_resp('{"slides": [{"id": "S0', finish_reason="length")]
    with pytest.raises(llm.LLMTruncatedError, match="截断.*800"):
        _call(schemas.SlidesResponse)
    assert len(fake_openai.calls) == 1
    assert db.fetch_one("SELECT COUNT(*) AS n FROM api_usage")["n"] == 1


# ---------- Anthropic 分支：JSON 模式 + 本地校验 ----------

def test_anthropic_provider_uses_json_fallback(tmp_settings, set_settings, monkeypatch):
    set_settings(dataclasses.replace(config.settings, anthropic_api_key="test-key"))
    assert llm.structured_mode() == "anthropic"
    seen = []
    replies = ['{"slides": [{"id": "S999"}]}', _slides_json()]

    def stream(**kw):
        seen.append(kw)
        text = replies.pop(0)
        msg = SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn",
                              usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0,
                                                    cache_creation_input_tokens=0))

        class S:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get_final_message(self):
                return msg
        return S()

    monkeypatch.setattr(llm, "_get_client", lambda: SimpleNamespace(messages=SimpleNamespace(stream=stream)))
    out = llm.complete_structured(messages=[{"role": "user", "content": "x"}], system=[{"type": "text", "text": "s"}],
                                  response_model=schemas.slides_response_model(**BATCH), video_id=None,
                                  stage="read_slides", cache_system=True)
    assert out.slides[0].id == "S012" and len(seen) == 2
    system = seen[0]["system"]
    assert system[0]["text"] == "s" and "JSON Schema" in system[-1]["text"]
    assert system[-1]["cache_control"] == {"type": "ephemeral"}
    assert "'S999'" in seen[1]["messages"][-1]["content"]


# ---------- ID 枚举与 schema 转换 ----------

def test_enum_model_rejects_unknown_ids():
    Model = schemas.slides_response_model(**BATCH)
    Model.model_validate_json(_slides_json())
    with pytest.raises(ValidationError, match="S013"):
        Model.model_validate_json(_slides_json(slide_id="S013"))
    with pytest.raises(ValidationError):
        Model.model_validate_json(_slides_json(t2="T0033"))
    # 基础模型接受任意字符串 ID
    schemas.SlidesResponse.model_validate_json(_slides_json(slide_id="S013"))
    # 本批没有讲解：speech_categories 只能为空
    Empty = schemas.slides_response_model(["S012"], ["S012.1", "S012.2"], [])
    with pytest.raises(ValidationError):
        Empty.model_validate_json(_slides_json())
    with pytest.raises(ValueError, match="不能为空"):
        schemas.id_enum([])
    assert schemas.id_enum(["a", "b", "a"]) == Literal["a", "b"]


def test_speech_categories_must_be_array():
    with pytest.raises(ValidationError):
        schemas.SlidesResponse.model_validate({"slides": [{
            "id": "S1", "title": "", "visual_content": "", "occurrences": [],
            "speech_categories": {"T0031": "lecture"}}]})
    items = [schemas.SpeechCategory(id="T1", category="lecture"), schemas.SpeechCategory(id="T1", category="admin"),
             schemas.SpeechCategory(id="T9", category="media")]
    problems = schemas.check_speech_categories(items, ["T1", "T2"])
    assert len(problems) == 3 and "T9" in problems[0] and "T1" in problems[1] and "T2" in problems[2]


def test_outline_schema_is_strict():
    s = openai_compat.strict_json_schema(schemas.OutlineResponse)
    assert s["required"] == ["chapters", "admin", "speech_categories"]
    chapter = s["$defs"]["OutlineChapter"]
    assert chapter["additionalProperties"] is False and "children" in chapter["required"]
    assert "children" not in s["$defs"]["OutlineItem"]["properties"]     # 最多两层
    assert "description" not in chapter                                  # 类注释不进 schema
    assert openai_compat.schema_limit_problems(s) == []


class _Inner(BaseModel):
    x: int


class _Tricky(BaseModel):
    title: str = "default title"
    maybe: Optional[str] = None
    kind: Literal["only"] = "only"
    inner: _Inner = Field(description="nested with description")
    inners: list[_Inner] = []


def test_strict_schema_handles_defaults_optional_const_and_refs():
    s = openai_compat.strict_json_schema(_Tricky)
    assert s["required"] == ["title", "maybe", "kind", "inner", "inners"]
    assert s["additionalProperties"] is False and "title" in s["properties"]
    text = json.dumps(s)
    assert '"default"' not in text and '"const"' not in text
    assert s["properties"]["maybe"] == {"anyOf": [{"type": "string"}, {"type": "null"}]}
    assert s["properties"]["kind"]["enum"] == ["only"]
    inner = s["properties"]["inner"]                                        # $ref 带 description → 内联
    assert "$ref" not in inner and inner["additionalProperties"] is False and inner["required"] == ["x"]
    assert s["$defs"]["_Inner"]["additionalProperties"] is False


def test_strict_schema_rejects_arbitrary_keys():
    class Bad(BaseModel):
        speech_categories: dict[str, str]

    with pytest.raises(TypeError, match="任意键"):
        openai_compat.strict_json_schema(Bad)
