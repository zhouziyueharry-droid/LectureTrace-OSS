"""Flex 处理档位：设置、请求体、429 资源不可用的退避与改用标准档、按实际档位计价、api_usage 记档位、发送清单估算。
全部用假客户端，不发真实请求。"""
from __future__ import annotations

import dataclasses
import logging
import sqlite3
from types import SimpleNamespace

import httpx2
import openai
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app import config, db, keys
from app.llm import client as llm, openai_compat, settings as llm_settings, tokens
from app.pipeline import send_plan

OFFICIAL = "https://api.openai.com/v1"
MSG = [{"role": "user", "content": "hi"}]


class Ok(BaseModel):
    ok: bool


def _status_error(status: int, *, code: str | None = None, message: str = "error", err_type: str | None = None):
    body = {"message": message, "type": err_type, "code": code, "param": None}
    resp = httpx2.Response(status, request=httpx2.Request("POST", f"{OFFICIAL}/chat/completions"),
                           json={"error": body})
    cls = openai.RateLimitError if status == 429 else openai.InternalServerError
    return cls(f"Error code: {status} - {{'error': {body}}}", response=resp, body=body)


def resource_unavailable():
    return _status_error(429, code="resource_unavailable", message="Resource unavailable. Please try again later.")


def rate_limited():
    return _status_error(429, code="rate_limit_exceeded", err_type="requests",
                         message="Rate limit reached for gpt-5.6-sol on requests per min (RPM)")


def _resp(content="ok", tier="flex", prompt=1000, completion=200, cached=0):
    usage = SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion,
                            prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
                            completion_tokens_details=SimpleNamespace(reasoning_tokens=0))
    msg = SimpleNamespace(content=content, refusal=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")], usage=usage,
                           service_tier=tier)


@pytest.fixture
def fake(tmp_settings, set_settings, monkeypatch):
    """OpenAI 官方接口 + 假客户端：fake.script 依次为异常或响应（为空时回应 tier 与请求一致的成功响应）；
    fake.calls 记录每次 create 的参数；fake.sleeps 记录退避等待。"""
    set_settings(dataclasses.replace(config.settings, llm_provider="openai"))
    keys.update({"openai": "sk-test", "openai_base_url": OFFICIAL})
    monkeypatch.delenv("OPENAI_STRUCTURED_OUTPUTS", raising=False)
    fake = SimpleNamespace(calls=[], script=[], sleeps=[])

    def create(**kw):
        fake.calls.append(kw)
        if fake.script:
            step = fake.script.pop(0)
            if isinstance(step, Exception):
                raise step
            return step
        return _resp(tier="flex" if kw.get("service_tier") == "flex" else "default")

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(openai_compat, "get_client", lambda: client)
    monkeypatch.setattr(openai_compat, "_sleep", fake.sleeps.append)
    monkeypatch.setattr(openai_compat, "_flex_fallback_at", None)
    return fake


# ---------- 设置 ----------

def test_default_tiers_and_update_validation(tmp_settings):
    s = llm_settings.get()
    assert s["service_tiers"] == {"read_slides": "flex", "outline": "flex", "qa": "standard"}
    assert s["service_tier_options"] == ["flex", "standard"]
    assert llm_settings.service_tier_for("translate") == "standard"          # 其他 stage 与选模型一样按 qa

    s = llm_settings.update({"service_tiers": {"outline": "standard", "qa": "flex", "unknown": "flex"}})
    assert s["service_tiers"] == {"read_slides": "flex", "outline": "standard", "qa": "flex"}
    with pytest.raises(ValueError, match="未知的处理档位"):
        llm_settings.update({"service_tiers": {"qa": "fast"}})             # 快速档（2 倍价）不开放
    assert llm_settings.get()["service_tiers"]["qa"] == "flex"              # 校验失败不改动

    db.set_setting("llm_settings", {"service_tiers": {"qa": "bogus"}})      # 库里的坏值退回默认
    assert llm_settings.service_tier_for("qa") == "standard"


def test_settings_api_accepts_service_tiers(tmp_settings, fake_stages):
    from app.main import app
    with TestClient(app) as c:
        llm_view = c.get("/api/settings").json()["llm"]
        assert llm_view["service_tiers"]["read_slides"] == "flex"
        assert llm_view["prices"]["gpt-5.6-sol"]["flex_input"] == 2.0
        r = c.put("/api/settings/llm", json={"service_tiers": {"read_slides": "standard"}})
        assert r.status_code == 200 and r.json()["llm"]["service_tiers"]["read_slides"] == "standard"
        assert c.put("/api/settings/llm", json={"service_tiers": {"qa": "priority"}}).status_code == 400


# ---------- 请求体 ----------

def test_build_request_adds_flex_only_on_official_api(tmp_settings):
    keys.update({"openai_base_url": OFFICIAL})
    kw = dict(model="gpt-5.6-sol", messages=MSG, system=None, max_tokens=10, effort="low", json_mode=False)
    assert openai_compat.build_request(**kw, service_tier="flex")["service_tier"] == "flex"
    assert "service_tier" not in openai_compat.build_request(**kw, service_tier="standard")
    assert "service_tier" not in openai_compat.build_request(**kw)
    keys.update({"openai_base_url": "https://api.deepseek.com"})
    assert "service_tier" not in openai_compat.build_request(**{**kw, "model": "deepseek-chat"}, service_tier="flex")
    assert openai_compat.effective_service_tier("read_slides") == "standard"


def test_stages_use_their_tier(fake):
    llm.complete(messages=MSG, video_id="v", stage="outline", effort="medium")
    req = fake.calls[-1]
    assert req["service_tier"] == "flex" and req["timeout"] == openai_compat.FLEX_TIMEOUT_S == 900.0

    llm.complete(messages=MSG, video_id="v", stage="qa")
    assert "service_tier" not in fake.calls[-1] and "timeout" not in fake.calls[-1]   # 问答走标准

    fake.script = [_resp('{"ok": true}')]
    llm.complete_structured(messages=MSG, response_model=Ok,
                            video_id="v", stage="read_slides", check=None)
    assert fake.calls[-1]["service_tier"] == "flex"
    assert fake.calls[-1]["response_format"]["type"] == "json_schema"

    llm_settings.update({"service_tiers": {"read_slides": "standard"}})
    llm.complete(messages=MSG, video_id="v", stage="read_slides")
    assert "service_tier" not in fake.calls[-1]


def test_compatible_api_never_sends_flex(fake):
    keys.update({"openai_base_url": "https://api.deepseek.com"})
    llm.complete(messages=MSG, video_id="v", stage="outline")
    assert "service_tier" not in fake.calls[-1] and "timeout" not in fake.calls[-1]


# ---------- 429 资源不可用 ----------

def test_identifies_resource_unavailable_not_rate_limit():
    ok = openai_compat.is_resource_unavailable
    assert ok(resource_unavailable())
    assert ok(_status_error(429, message="Resource Unavailable"))                 # 没有 code，只看文字
    assert ok(_status_error(429, err_type="resource_unavailable"))
    assert not ok(rate_limited())
    assert not ok(_status_error(429, code="insufficient_quota", message="You exceeded your current quota"))
    # 普通限流的 code 优先：即使文字里碰巧出现也不算
    assert not ok(_status_error(429, code="slow_down", message="resource unavailable, slow down"))
    assert not ok(_status_error(500, message="resource unavailable"))
    assert not ok(RuntimeError("resource unavailable"))


def test_resource_unavailable_backs_off_then_falls_back_to_standard(fake, caplog):
    n = len(openai_compat.FLEX_RETRY_DELAYS_S) + 1
    fake.script = [resource_unavailable() for _ in range(n)] + [_resp("好", tier="default")]
    with caplog.at_level(logging.WARNING, logger=openai_compat.log.name):
        out = llm.complete(messages=MSG, video_id="v1", stage="read_slides")
    assert out == "好"
    assert [c.get("service_tier") for c in fake.calls] == ["flex"] * n + ["default"]
    assert fake.sleeps == list(openai_compat.FLEX_RETRY_DELAYS_S)
    assert "timeout" not in fake.calls[-1]
    assert any("改用标准档" in r.getMessage() for r in caplog.records)
    # 资源不可用不收费、不记账；只记最后成功的一次，按标准价
    rows = db.fetch_all("SELECT * FROM api_usage")
    assert len(rows) == 1 and rows[0]["service_tier"] == "default"
    assert rows[0]["cost_usd"] == pytest.approx((1000 * 4 + 200 * 20) / 1e6)


def test_flex_recovers_during_backoff(fake):
    fake.script = [resource_unavailable(), _resp("ok", tier="flex")]
    assert llm.complete(messages=MSG, video_id="v", stage="outline") == "ok"
    assert [c["service_tier"] for c in fake.calls] == ["flex", "flex"] and fake.sleeps == [10.0]
    assert db.fetch_one("SELECT service_tier FROM api_usage")["service_tier"] == "flex"


def test_cooldown_skips_long_backoff_after_fallback(fake):
    n = len(openai_compat.FLEX_RETRY_DELAYS_S) + 1
    fake.script = [resource_unavailable() for _ in range(n)] + [_resp(tier="default")]
    llm.complete(messages=MSG, video_id="v", stage="read_slides")
    fake.calls.clear(), fake.sleeps.clear()
    # 冷却期内：flex 只试一轮（SDK 自带短重试在真实客户端里），仍失败就直接改用标准档
    fake.script = [resource_unavailable(), _resp(tier="default")]
    llm.complete(messages=MSG, video_id="v", stage="read_slides")
    assert [c["service_tier"] for c in fake.calls] == ["flex", "default"] and fake.sleeps == []
    # 冷却期内的下一批仍先试 flex（有容量就按半价）
    fake.calls.clear()
    llm.complete(messages=MSG, video_id="v", stage="read_slides")
    assert [c["service_tier"] for c in fake.calls] == ["flex"]


def test_ordinary_rate_limit_is_not_a_fallback(fake):
    fake.script = [rate_limited()]
    with pytest.raises(llm.LLMError, match="限流"):
        llm.complete(messages=MSG, video_id="v", stage="read_slides")
    assert len(fake.calls) == 1 and fake.sleeps == []
    assert openai_compat._flex_fallback_at is None
    assert db.fetch_one("SELECT COUNT(*) AS n FROM api_usage")["n"] == 0


def test_structured_call_falls_back_too(fake):
    n = len(openai_compat.FLEX_RETRY_DELAYS_S) + 1
    fake.script = [resource_unavailable() for _ in range(n)] + [_resp('{"ok": true}', tier="default")]
    out = llm.complete_structured(messages=MSG, response_model=Ok,
                                  video_id="v", stage="read_slides")
    assert out.ok is True and fake.calls[-1]["service_tier"] == "default"
    assert fake.calls[-1]["response_format"]["type"] == "json_schema"             # 其余参数不变


def test_stream_falls_back_and_records_tier(fake):
    llm_settings.update({"service_tiers": {"qa": "flex"}})

    def chunks(tier):
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=10,
                                prompt_tokens_details=SimpleNamespace(cached_tokens=0),
                                completion_tokens_details=SimpleNamespace(reasoning_tokens=0))
        delta = lambda t: SimpleNamespace(delta=SimpleNamespace(content=t))  # noqa: E731
        return iter([SimpleNamespace(choices=[delta("你")], usage=None, service_tier=tier),
                     SimpleNamespace(choices=[delta("好")], usage=None, service_tier=tier),
                     SimpleNamespace(choices=[], usage=usage, service_tier=tier)])

    n = len(openai_compat.FLEX_RETRY_DELAYS_S) + 1
    fake.script = [resource_unavailable() for _ in range(n)] + [chunks("default")]
    assert "".join(llm.stream(messages=MSG, video_id="v", stage="qa")) == "你好"
    assert fake.calls[0]["stream"] is True and fake.calls[-1]["service_tier"] == "default"
    assert db.fetch_one("SELECT service_tier FROM api_usage")["service_tier"] == "default"

    fake.script = [chunks("flex")]
    "".join(llm.stream(messages=MSG, video_id="v", stage="qa"))
    row = db.fetch_one("SELECT * FROM api_usage ORDER BY id DESC LIMIT 1")
    assert row["service_tier"] == "flex" and row["cost_usd"] == pytest.approx((100 * 2 + 10 * 10) / 1e6)


# ---------- 计价 ----------

def test_cost_by_actual_tier(tmp_settings):
    # gpt-5.6-sol Flex：$2 / 缓存 $0.20 / 写入 $2.50 / 输出 $10
    assert tokens.cost_usd("gpt-5.6-sol", 1000, 100, cached_tokens=200, cache_write_tokens=300,
                           service_tier="flex") == pytest.approx((500 * 2 + 200 * 0.2 + 300 * 2.5 + 100 * 10) / 1e6)
    assert tokens.cost_usd("gpt-5.6-sol", 1000, 100, service_tier="default") == pytest.approx((1000 * 4 + 100 * 20) / 1e6)
    assert tokens.cost_usd("gpt-5.6-sol", 1000, 100, service_tier=None) == tokens.cost_usd("gpt-5.6-sol", 1000, 100)
    # 长上下文同样减半
    assert tokens.cost_usd("gpt-5.6-sol", 300_000, 0, service_tier="flex") == pytest.approx(300_000 * 4 / 1e6)
    assert tokens.cost_usd("gpt-5.6-luna", 200_000, 1_000_000, service_tier="flex") == pytest.approx(0.02 + 0.60)
    assert tokens.cost_usd("gpt-5.6-luna", 1_000_000, 1_000_000, service_tier="flex") == pytest.approx(0.20 + 0.90)
    # OpenAI 默认价格的 flex 价都是标准价的一半（官方 2026-09-28）；DeepSeek 无 Flex。
    for model, p in llm_settings.DEFAULT_PRICES.items():
        if model == 'deepseek-flash':
            assert not tokens.has_flex_price(model)
            continue
        for k in ("input", "cached_input", "output", "long_input", "long_cached_input", "long_output"):
            if k in p:  # gpt-6 系列未收录长上下文价格
                assert p[f"flex_{k}"] == pytest.approx(p[k] / 2), (model, k)
    # 自定义价格没有 flex 价：按标准价（宁可高估）
    llm_settings.update({"prices": {"my-model": {"input": 1, "output": 2}}})
    assert tokens.cost_usd("my-model", 1_000_000, 0, service_tier="flex") == pytest.approx(1.0)
    assert not tokens.has_flex_price("my-model") and tokens.has_flex_price("gpt-5.6-sol")


def test_usage_records_actual_tier_and_flex_price(fake):
    fake.script = [_resp(tier="flex", cached=400)]
    llm.complete(messages=MSG, video_id="v1", stage="outline")
    row = db.fetch_one("SELECT * FROM api_usage")
    assert row["service_tier"] == "flex" and row["cost_usd"] == pytest.approx((600 * 2 + 400 * 0.2 + 200 * 10) / 1e6)

    # 请求了 flex 但响应没带档位（兼容接口或老版本）：记 NULL，按标准价
    fake.script = [_resp(tier=None)]
    llm.complete(messages=MSG, video_id="v2", stage="outline")
    row = db.fetch_one("SELECT * FROM api_usage WHERE video_id = 'v2'")
    assert row["service_tier"] is None and row["cost_usd"] == pytest.approx((1000 * 4 + 200 * 20) / 1e6)


def test_init_db_adds_service_tier_column_to_old_table(tmp_settings):
    path = config.settings.db_path
    path.unlink()
    conn = sqlite3.connect(path)       # 旧版 api_usage：没有 service_tier 列，且已有数据
    conn.execute("CREATE TABLE api_usage (id INTEGER PRIMARY KEY AUTOINCREMENT, video_id TEXT, stage TEXT,"
                 " provider TEXT NOT NULL, model TEXT, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,"
                 " cache_read_tokens INTEGER DEFAULT 0, images INTEGER DEFAULT 0, cost_usd REAL DEFAULT 0,"
                 " created_at TEXT NOT NULL)")
    conn.execute("INSERT INTO api_usage (provider, created_at) VALUES ('DeepL', 'x')")
    conn.commit()
    conn.close()
    db.init_db()
    db.init_db()                       # 已存在就跳过
    cols = [r["name"] for r in db.fetch_all("PRAGMA table_info(api_usage)")]
    assert cols.count("service_tier") == 1
    assert db.fetch_one("SELECT service_tier FROM api_usage")["service_tier"] is None
    db.record_usage(video_id="v", stage="qa", provider="OpenAI 兼容", model="m", service_tier="flex")
    assert db.fetch_one("SELECT service_tier FROM api_usage WHERE video_id = 'v'")["service_tier"] == "flex"


# ---------- 发送清单 ----------

def test_send_plan_prices_by_tier(tmp_settings, set_settings):
    set_settings(dataclasses.replace(config.settings, llm_provider="openai"))
    keys.update({"openai_base_url": OFFICIAL})
    plan = {"warnings": [], "blocking": []}
    est = {"model": "gpt-5.6-sol", "effort": "low", "requests": [(10_000, 2_000), (8_000, 1_000)],
           "output_range": (1_000, 3_000), "desc": "d"}

    flex = send_plan._priced("read_slides", dict(est), plan)
    # 档位与最坏情况由前端按 service_tier、fallback_cost_* 显示，desc / provider 不再重复写
    assert flex["service_tier"] == "flex" and "资源不足" not in flex["desc"]
    llm_settings.update({"service_tiers": {"read_slides": "standard"}})
    std = send_plan._priced("read_slides", dict(est), plan)
    assert std["service_tier"] == "standard" and "fallback_cost_usd_max" not in std
    assert "资源不足" not in std["desc"]
    # Flex 半价（均在长上下文阈值以下）；最坏情况 = 全部按标准价的上限
    assert flex["cost_usd_range"] == [pytest.approx(v / 2, abs=1e-4) for v in std["cost_usd_range"]]
    assert flex["fallback_cost_usd_max"] == std["cost_usd_range"][1]
    assert plan["warnings"] == []

    # 兼容接口：设置为 flex 也按标准价，并说明
    llm_settings.update({"service_tiers": {"read_slides": "flex"}})
    keys.update({"openai_base_url": "https://api.deepseek.com"})
    compat = send_plan._priced("read_slides", dict(est), plan)
    assert compat["service_tier"] == "standard" and compat["cost_usd_range"] == std["cost_usd_range"]
    assert "Flex 档不生效" in compat["desc"]


def test_send_plan_totals_include_fallback_max(tmp_settings, set_settings, monkeypatch):
    set_settings(dataclasses.replace(config.settings, llm_provider="openai",
                                     translate=config.TranslateParams(provider="deepl")))
    keys.update({"deepl": "k:fx", "openai": "sk-test", "openai_base_url": OFFICIAL})
    monkeypatch.setattr(send_plan, "deepl_usage", lambda key: None)
    est = {"model": "gpt-5.6-sol", "effort": "low", "requests": [(10_000, 0)], "output_range": (1_000, 2_000),
           "desc": "d"}
    monkeypatch.setattr(send_plan, "_read_slides_estimate", lambda *a: dict(est))
    monkeypatch.setattr(send_plan, "_outline_estimate", lambda *a: dict(est))
    from app import artifacts
    artifacts.write_artifact("vflex", "keyframes.json", [{"id": "S001", "image": "x.jpg", "appearances": [[0, 1]]}], {})
    artifacts.write_artifact("vflex", "cues.json", [{"id": "C0001", "start": 0, "end": 1, "text": "a", "zh": "译"}], {})
    plan = send_plan.build_send_plan("vflex")
    items = {i["stage"]: i for i in plan["items"]}
    assert items["read_slides"]["service_tier"] == items["outline"]["service_tier"] == "flex"
    worst = items["read_slides"]["fallback_cost_usd_max"] + items["outline"]["fallback_cost_usd_max"]
    assert plan["fallback_cost_usd_max"] == pytest.approx(worst, abs=1e-4)
    assert plan["fallback_cost_usd_max"] > plan["cost_usd_range"][1]
    assert plan["fallback_cost_sgd_max"] == round(plan["fallback_cost_usd_max"] * plan["usd_to_sgd"], 2)
    assert plan["flex_prices_as_of"] == "2026-09-28"
